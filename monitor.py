#!/usr/bin/env python3
"""Daily Japanese Pokemon TCG purchase-information monitor."""
import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from adapters import SITE, stock_status, canonical_product
from events import (JST, SOURCES, identity, pokeget_offers, mirar_offers,
                    board_offers, cardchusen_offers, observation, plan_events)

from toretoku import fetch_posts as toretoku_posts, plan_announcements

from source_http import SourceFetcher, SourceDeferred, safe_reason

UA = "PokecaNotify/1.0 (+https://github.com/hiyokko/pokeca-notify)"


def request(url, payload=None, headers=None, method=None):
    data = None if payload is None else json.dumps(payload).encode()
    h = {"User-Agent": UA, "Accept": "application/json,text/html", **(headers or {})}
    if data is not None:
        h["Content-Type"] = "application/json"
    with urlopen(Request(url, data=data, headers=h, method=method), timeout=20) as response:
        body = response.read(8_000_001)
        if len(body) > 8_000_000:
            raise ValueError("応答サイズが上限を超えました")
        return body.decode(response.headers.get_content_charset() or "utf-8"), response.headers


def get_json(url):
    text, headers = request(url)
    return json.loads(text), headers


def safe_error(error):
    # Never log request URLs/headers: they may contain the Discord webhook token.
    if isinstance(error, SourceDeferred):
        return "再試行待機中"
    if isinstance(error, HTTPError):
        return "HTTP {}".format(error.code)
    return type(error).__name__


def fresh_state():
    return {"version": 1, "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "posts": {}, "records": {}, "stock": {}, "events": {}, "initialized_sources": []}


def validate_state(state):
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ValueError("状態ファイルの形式が不正です。初期化せず停止します")
    for name in ("posts", "records", "stock"):
        if not isinstance(state.get(name), dict):
            raise ValueError("状態ファイルが破損しています")
    if not isinstance(state.get("started_at"), str):
        raise ValueError("開始日時がありません")
    for name in ('events',):
        if not isinstance(state.get(name, {}), dict):
            raise ValueError('購入イベント状態が破損しています')
    if not isinstance(state.get('initialized_sources', []), list):
        raise ValueError('ソース状態が破損しています')
    if 'toretoku_started_at' in state:
        if datetime.fromisoformat(state['toretoku_started_at']).tzinfo is None:
            raise ValueError('トレトクの開始日時が不正です')
    for name in ('source_retry_after', 'source_status', 'last_check', 'product_catalog'):
        if not isinstance(state.get(name, {}), dict):
            raise ValueError('情報源状態が破損しています')
    return state


class StateStore:
    def __init__(self, path=".state.json", github=False):
        self.path, self.github, self.sha = Path(path), github, None
        if github:
            repo = os.environ["GITHUB_REPOSITORY"]
            if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
                raise ValueError("Invalid repository")
            self.base = "https://api.github.com/repos/" + repo
            self.headers = {"Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
                            "Accept": "application/vnd.github+json",
                            "X-GitHub-Api-Version": "2022-11-28"}

    def api(self, path, payload=None, method=None):
        text, _ = request(self.base + path, payload, self.headers, method)
        return json.loads(text)

    def load(self):
        if not self.github:
            return validate_state(json.loads(self.path.read_text())) if self.path.exists() else fresh_state()
        try:
            data = self.api("/contents/state.json?ref=monitor-state")
        except HTTPError as error:
            if error.code != 404:
                raise
            # Missing file on an existing state branch indicates data loss.
            try:
                self.api("/git/ref/heads/monitor-state")
            except HTTPError as branch_error:
                if branch_error.code == 404:
                    return fresh_state()
                raise
            raise ValueError("monitor-stateに状態ファイルがありません。自動初期化を中止します")
        self.sha = data["sha"]
        return validate_state(json.loads(base64.b64decode(data["content"])))

    def save(self, state):
        validate_state(state)
        text = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
        if not self.github:
            temp = self.path.with_suffix(".tmp")
            temp.write_text(text)
            temp.replace(self.path)
            return
        if self.sha is None:
            # Create a dedicated branch at the workflow's verified commit.
            self.api("/git/refs", {"ref": "refs/heads/monitor-state", "sha": os.environ["GITHUB_SHA"]})
        payload = {"message": "Update daily monitor state", "branch": "monitor-state",
                   "content": base64.b64encode(text.encode()).decode()}
        if self.sha:
            payload["sha"] = self.sha
        data = self.api("/contents/state.json", payload, "PUT")
        self.sha = data["content"]["sha"]


def send_discord(records, webhook):
    if not re.fullmatch(r"https://(?:canary\.|ptb\.)?discord\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+", webhook):
        raise ValueError("DISCORD_WEBHOOK_URLの形式を確認してください")
    payload = {"username": "ポケカ通知", "allowed_mentions": {"parse": []},
               "content": "🃏 ポケカ日次チェック",
               "embeds": [{"title": ("【" + r.get("event", "購入機会") + "】" + r["title"])[:220],
                           "url": r["url"], "description": r.get("note", "")[:500],
                           "color": 16763904,
                           "footer": {"text": r.get("retailer", "") + " · " + r.get("source", "")}}
                          for r in records]}
    for attempt in range(3):
        try:
            text, _ = request(webhook + "?wait=true", payload)
            result = json.loads(text)
            if not result.get("id"):
                raise ValueError("Discordの送信確認を取得できません")
            return
        except HTTPError as error:
            if error.code != 429 or attempt == 2:
                raise
            try:
                delay = float(json.loads(error.read()).get("retry_after", 5))
            except (ValueError, TypeError):
                delay = 5
            if delay < 0 or delay > 60:
                raise ValueError("Discordの待機時間が上限を超えました")
            time.sleep(delay + 0.2)
    raise RuntimeError("Discord delivery failed")


def post_index(fetch_json=None):
    fetch_json = fetch_json or get_json
    # Category resolved by slug rather than relying on a permanently stable ID.
    categories, _ = fetch_json(SITE + "/wp-json/wp/v2/categories?slug=resale-information")
    if len(categories) != 1:
        raise ValueError("再販・抽選カテゴリを取得できません")
    posts, page = [], 1
    while True:
        params = {"categories": categories[0]["id"], "per_page": 100, "page": page,
                  "orderby": "modified", "_fields": "id,link,title,modified_gmt"}
        batch, headers = fetch_json(SITE + "/wp-json/wp/v2/posts?" + urlencode(params))
        if not isinstance(batch, list) or not batch:
            raise ValueError("記事一覧が空です")
        posts.extend(batch)
        total_pages = int(headers.get("X-WP-TotalPages", "0"))
        if not total_pages or total_pages > 50:
            raise ValueError("記事一覧のページ情報が不正です")
        if page >= total_pages:
            return posts
        page += 1
        time.sleep(0.5)


def write_report(report):
    path = Path('report.json')
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    temp.replace(path)


def known_product_urls(state, offers):
    # Saved URLs are discovery only. Their old availability/lottery votes are not reused.
    candidates = list(state.get('product_catalog', {}).values()) + [
        o for p in state['posts'].values() if p.get('kind') in {'box', 'box_set'}
        for o in p.get('offers', [])] + list(offers)
    products = {}
    for offer in candidates:
        url = canonical_product(offer.get('url', ''))
        if url and urlsplit(url).hostname == 'www.amazon.co.jp':
            products[url] = dict(offer, url=url)
    return products


def run(args):
    store = StateStore(args.state, args.github_state)
    state = store.load()
    webhook = os.environ.get('DISCORD_WEBHOOK_URL', '')
    if not args.dry_run and not webhook:
        raise ValueError('DISCORD_WEBHOOK_URLが未設定です')
    config = json.loads(Path('config.json').read_text())
    if args.test_notification:
        event = {'title': '接続テスト', 'url': 'https://github.com/' + os.environ.get('GITHUB_REPOSITORY', 'hiyokko/pokeca-notify'),
                 'event': 'テスト', 'retailer': 'Amazon・ポケモンセンターオンライン',
                 'note': '日次チェックは毎朝9時（日本時間）。BOX／BOX入りセットの新しい抽選開始・再販と、トレトクのポケモンPSAオリパ販売告知を通知します。記事更新だけでは通知しません。'}
        if not args.dry_run:
            send_discord([event], webhook)
        print('接続テスト完了（dry-run: {}）'.format(args.dry_run))
        return 0
    now = datetime.now(JST)
    fetcher = SourceFetcher(request, state.get('source_retry_after', {}))
    errors, successful, offers, source_counts, statuses = [], [], [], {}, {}
    previous = state.get('source_status', {})

    def status(name, ok, count, articles=None, cached_articles=None, error=None):
        before = previous.get(name, {})
        value = {'status': 'ok' if ok else 'failed', 'checked_at': now.isoformat(),
                 'offers': count, 'last_success_at': now.isoformat() if ok else before.get('last_success_at')}
        if articles is not None:
            value['articles'] = articles
        if cached_articles is not None:
            value['cached_articles'] = cached_articles
        if error:
            value['error'] = safe_reason(error)
        statuses[name] = value
        if ok:
            source_counts[name] = count

    scanned, index_error = 0, None
    try:
        index = post_index(fetcher.json)
    except Exception as error:
        index_error = error
        errors.append('ポケゲト記事一覧: ' + safe_error(error))
        index = []
    pokeget_ok = bool(index)
    for meta in index:
        key = str(meta['id'])
        cached = state['posts'].get(key)
        if cached and cached.get('modified') == meta['modified_gmt'] and 'offers' in cached:
            offers.extend(cached['offers'])
            continue
        try:
            post, _ = fetcher.json(SITE + '/wp-json/wp/v2/posts/{}?_fields=id,link,title,content,modified_gmt'.format(meta['id']))
            found, catalog = pokeget_offers(post)
            state['posts'][key] = {**catalog, 'modified': meta['modified_gmt'], 'offers': found}
            offers.extend(found)
            scanned += 1
        except Exception as error:
            pokeget_ok, index_error = False, error
            errors.append('記事 {}: {}'.format(key, safe_error(error)))
        time.sleep(0.3)
    if pokeget_ok:
        successful.append('ポケゲトちゃんねる')
    status('ポケゲトちゃんねる', pokeget_ok, len(offers), len(index), len(state['posts']), index_error)
    known = {identity(p['title']) for p in state['posts'].values() if p['kind'] in {'box', 'box_set'}}
    for source_id in ('mirar', 'board', 'cardchusen'):
        if source_id not in config.get('summary_sources', []):
            continue
        name, url = SOURCES[source_id]
        try:
            html, _ = fetcher.get(url)
            parser = {'mirar': mirar_offers, 'board': board_offers, 'cardchusen': cardchusen_offers}[source_id]
            found = parser(html) if source_id == 'mirar' else parser(html, known)
            offers.extend(found)
            known.update(o['product'] for o in found)
            successful.append(name)
            status(name, True, len(found))
        except Exception as error:
            status(name, False, 0, error=error)
            errors.append(name + ': ' + safe_error(error))
    products = known_product_urls(state, offers)
    state['product_catalog'] = products
    unknown = 0
    if config.get('check_amazon_stock', False):
        for url, offer in products.items():
            try:
                html, _ = request(url)
                stock = stock_status(html, url)
            except Exception:
                stock = 'unknown'
            if stock == 'unknown':
                unknown += 1
            else:
                offers.append(observation(offer['title'], 'Amazon', 'sale', 'Amazon公式',
                                          url, url, 'open' if stock == 'available' else 'closed', direct=True))
                successful.append('Amazon公式')
            time.sleep(0.5)
    pending = plan_events(state, offers, now, successful)
    toretoku_count = 0
    if config.get('check_toretoku_psa_oripa', False):
        try:
            posts = toretoku_posts(fetcher.json)
            announcements, count = plan_announcements(state, posts, now)
            pending.extend(announcements)
            toretoku_count = len(posts)
            status('トレトク公式ブログ', True, count, toretoku_count)
        except Exception as error:
            status('トレトク公式ブログ', False, 0, 0, error=error)
            errors.append('トレトク公式ブログ: ' + safe_error(error))
    retry_times = [t for host, t in fetcher.retry_after.items()
                   if datetime.fromisoformat(t) > datetime.now(JST) and
                   (fetcher.stats.get(host, {}).get('failed_requests') or fetcher.stats.get(host, {}).get('deferred_requests'))]
    retry_at = max(retry_times, key=datetime.fromisoformat) if errors and retry_times else None
    report = {'checked_at': now.isoformat(), 'complete': False,
              'run_id': os.environ.get('GITHUB_RUN_ID', ''),
              'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT', '1'),
              'delivered_notifications': 0, 'updated_articles': scanned,
              'catalog_articles': len(state['posts']), 'source_offers': source_counts,
              'source_status': statuses, 'http_retries': fetcher.stats,
              'retry_not_before': retry_at, 'toretoku_articles': toretoku_count,
              'tracked_purchase_events': len(state.get('events', {})),
              'product_urls': len(products), 'stock_unknown': unknown,
              'notifications': len(pending), 'errors': errors,
              'limits': '記事更新は通知しません。期間・販売状態を判定できたBOXのみ。全商品や短時間在庫は網羅しません。ポケセン在庫の直接確認は未対応。トレトクPSAオリパは公式の販売告知が対象で、在庫の直接確認はしません。'}
    state['source_retry_after'] = fetcher.retry_after
    state['source_status'] = statuses
    state['last_check'] = {k: report[k] for k in ('run_id', 'run_attempt', 'checked_at', 'retry_not_before', 'errors')}
    write_report(report)
    if not args.dry_run:
        store.save(state)
    for offset in range(0, len(pending), 5):
        batch = pending[offset:offset + 5]
        if args.dry_run:
            for record in batch:
                print(record['event'], record['retailer'], record['title'])
        else:
            send_discord(batch, webhook)
            report['delivered_notifications'] += len(batch)
            write_report(report)
            for record in batch:
                state['events'][record['key']] = record['after']
            store.save(state)
    report['complete'] = True
    report['completed_at'] = datetime.now(JST).isoformat()
    write_report(report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as file:
            file.write('## ポケカ日次チェック\n\n```json\n' + json.dumps(report, ensure_ascii=False, indent=2) + '\n```\n')
    return 1 if errors else 0

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default=".state.json")
    parser.add_argument("--github-state", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-notification", action="store_true")
    args = parser.parse_args()
    try:
        return run(args)
    except Exception as error:
        print("処理失敗: " + safe_error(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
