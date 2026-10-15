#!/usr/bin/env python3
"""Daily Japanese Pokemon TCG purchase-information monitor."""
import argparse
import base64
import copy
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

from adapters import SITE, OFFICIAL, article_records, official_records, stock_status

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
    if isinstance(error, HTTPError):
        return "HTTP {}".format(error.code)
    return type(error).__name__


def fresh_state():
    return {"version": 1, "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "posts": {}, "records": {}, "stock": {}, "official_initialized": False}


def validate_state(state):
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ValueError("状態ファイルの形式が不正です。初期化せず停止します")
    for name in ("posts", "records", "stock"):
        if not isinstance(state.get(name), dict):
            raise ValueError("状態ファイルが破損しています")
    if not isinstance(state.get("started_at"), str):
        raise ValueError("開始日時がありません")
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
               "embeds": [{"title": ("【" + r.get("event", "情報更新") + "】" + r["title"])[:220],
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


def post_index():
    # Category resolved by slug rather than relying on a permanently stable ID.
    categories, _ = get_json(SITE + "/wp-json/wp/v2/categories?slug=resale-information")
    if len(categories) != 1:
        raise ValueError("再販・抽選カテゴリを取得できません")
    posts, page = [], 1
    while True:
        params = {"categories": categories[0]["id"], "per_page": 100, "page": page,
                  "orderby": "modified", "_fields": "id,link,title,modified_gmt"}
        batch, headers = get_json(SITE + "/wp-json/wp/v2/posts?" + urlencode(params))
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


def changed_record(state, record, baseline=False):
    old = state["records"].get(record["key"])
    changed = not baseline and old != record["signature"]
    if changed:
        record["event"] = "新着情報" if old is None else "情報更新"
    return changed


def stock_transition(state, url, status):
    if status == "unknown":
        return False
    previous = state["stock"].get(url)
    state["stock"][url] = status
    return status == "available" and previous == "unavailable"


def run(args):
    store = StateStore(args.state, args.github_state)
    state = store.load()
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not args.dry_run and not webhook:
        raise ValueError("DISCORD_WEBHOOK_URLが未設定です")
    config = json.loads(Path("config.json").read_text())
    pending, errors, scanned = [], [], 0
    if args.test_notification:
        event = {"title": "接続テスト", "url": "https://github.com/" + os.environ.get("GITHUB_REPOSITORY", "hiyokko/pokeca-notify"),
                 "event": "テスト", "retailer": "Amazon・ポケモンセンターオンライン",
                 "note": "Discordへの接続を確認しました。日次チェックは毎朝9時（日本時間）。記事の更新と確認できた在庫変化を通知します。"}
        if not args.dry_run:
            send_discord([event], webhook)
        print("接続テスト完了（dry-run: {}）".format(args.dry_run))
        return 0
    try:
        index = post_index()
    except Exception as error:
        errors.append("ポケゲト記事一覧: " + safe_error(error))
        index = []
    for meta in index:
        key = str(meta["id"])
        cached = state["posts"].get(key)
        if cached and cached.get("modified") == meta["modified_gmt"]:
            continue
        try:
            post, _ = get_json(SITE + "/wp-json/wp/v2/posts/{}?_fields=id,link,title,content,modified_gmt".format(meta["id"]))
            records, catalog = article_records(post)
            baseline = not cached and meta["modified_gmt"] <= state["started_at"]
            for record in records:
                if changed_record(state, record, baseline):
                    pending.append(record)
                else:
                    state["records"][record["key"]] = record["signature"]
            state["posts"][key] = {**catalog, "modified": meta["modified_gmt"], "records": records}
            scanned += 1
        except Exception as error:
            errors.append("記事 {}: {}".format(key, safe_error(error)))
        time.sleep(0.3)
    # Retry records left pending after a previous delivery failure, even if the
    # article's modified timestamp did not change.
    pending_keys = {r["key"] for r in pending}
    for post in state["posts"].values():
        for record in post.get("records", []):
            if record["key"] not in pending_keys and changed_record(state, record):
                pending.append(record)
                pending_keys.add(record["key"])
    try:
        html, _ = request(OFFICIAL)
        records = official_records(html)
        for record in records:
            if changed_record(state, record, not state["official_initialized"]):
                pending.append(record)
            else:
                state["records"][record["key"]] = record["signature"]
        state["official_initialized"] = True
    except Exception as error:
        errors.append("公式ニュース: " + safe_error(error))
    products = {}
    for post in state["posts"].values():
        for record in post.get("records", []):
            for url in record.get("products", []):
                products[url] = record
    unknown = 0
    if config.get("check_amazon_stock", False):
        for url, record in products.items():
            if urlsplit(url).hostname != "www.amazon.co.jp":
                continue
            try:
                html, _ = request(url)
                status = stock_status(html, url)
            except Exception:
                status = "unknown"
            if status == "unknown":
                unknown += 1
                continue
            before = copy.deepcopy(state["stock"])
            if stock_transition(state, url, status):
                # Keep the old state until Discord confirms this transition.
                state["stock"] = before
                pending.append({**record, "key": "stock:" + url, "url": url,
                                "event": "在庫復活", "stock_url": url,
                                "note": "Amazon.co.jp販売・購入可能表示を確認。販売単位と価格を購入前に確認してください。"})
            time.sleep(0.5)
    if not args.dry_run:
        store.save(state)
    for offset in range(0, len(pending), 5):
        batch = pending[offset:offset + 5]
        if args.dry_run:
            for record in batch:
                print(record["event"], record["retailer"], record["title"])
        else:
            send_discord(batch, webhook)
            for record in batch:
                if record.get("stock_url"):
                    state["stock"][record["stock_url"]] = "available"
                else:
                    state["records"][record["key"]] = record["signature"]
            store.save(state)
    catalog = [{"title": p["title"], "url": p["url"], "kind": p["kind"]} for p in state["posts"].values()]
    report = {"updated_articles": scanned, "catalog_articles": len(catalog),
              "box_articles": sum(p["kind"] in {"box", "box_set"} for p in catalog),
              "review_articles": sum(p["kind"] == "review" for p in catalog),
              "product_urls": len(products), "stock_unknown": unknown,
              "notifications": len(pending), "errors": errors,
              "limits": "発見した記事・商品が対象。全BOXの網羅、記事に未掲載の販売、確認間の短時間在庫は保証しません。ポケセン在庫の直接確認は未対応。"}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    Path("report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as file:
            file.write("## ポケカ日次チェック\n\n```json\n" + json.dumps(report, ensure_ascii=False, indent=2) + "\n```\n")
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
