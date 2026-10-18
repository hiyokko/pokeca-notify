#!/usr/bin/env python3
"""Daily status to webhook 2; runs even if the monitor/test step failed."""
import base64
import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError

from watchdog import GitHubClient, safe_error, send_discord

JST = timezone(timedelta(hours=9))


def fresh_health():
    return {'version': 1, 'days': {}}


def validate_health(state):
    if not isinstance(state, dict) or state.get('version') != 1 or not isinstance(state.get('days'), dict):
        raise ValueError('Invalid health state')
    for day, entry in state['days'].items():
        datetime.strptime(day, '%Y-%m-%d')
        if not isinstance(entry, dict) or not isinstance(entry.get('attempts'), dict):
            raise ValueError('Invalid health history')
        for counts in entry['attempts'].values():
            if not isinstance(counts, dict) or any(type(counts.get(k)) is not int or counts[k] < 0 for k in ('detected', 'delivered')):
                raise ValueError('Invalid health counts')
    return state


class HealthStore:
    branch = 'health-state'

    def __init__(self, client):
        self.client, self.sha = client, None

    def load(self):
        try:
            result = self.client.api('/contents/health.json?ref=' + self.branch)
        except HTTPError as error:
            if error.code != 404:
                raise
            try:
                self.client.api('/git/ref/heads/' + self.branch)
            except HTTPError as branch_error:
                if branch_error.code == 404:
                    return fresh_health()
                raise
            raise ValueError('Existing health branch is missing its state')
        self.sha = result['sha']
        return validate_health(json.loads(base64.b64decode(result['content'])))

    def save(self, state):
        validate_health(state)
        if self.sha is None:
            self.client.api('/git/refs', {'ref': 'refs/heads/' + self.branch, 'sha': os.environ['GITHUB_SHA']})
        payload = {'message': 'Update daily health notification state', 'branch': self.branch,
                   'content': base64.b64encode((json.dumps(state, ensure_ascii=False, indent=2) + '\n').encode()).decode()}
        if self.sha:
            payload['sha'] = self.sha
        result = self.client.api('/contents/health.json', payload, 'PUT')
        self.sha = result['content']['sha']


def load_report(path, run_id, attempt):
    try:
        report = json.loads(Path(path).read_text())
        if (not isinstance(report, dict) or report.get('run_id') != str(run_id)
                or report.get('run_attempt') != str(attempt)):
            return None
        required = ('notifications', 'delivered_notifications', 'product_urls', 'stock_unknown')
        if any(type(report.get(k)) is not int or report[k] < 0 for k in required):
            return None
        if not isinstance(report.get('errors'), list) or not isinstance(report.get('source_offers'), dict):
            return None
        return report
    except (OSError, ValueError, TypeError):
        return None


def plan_health(state, report, outcome, now, repo, run_id, attempt):
    after = copy.deepcopy(validate_health(state))
    day = now.astimezone(JST).date().isoformat()
    entry = after['days'].setdefault(day, {'attempts': {}, 'signature': None})
    run_key = '{}:{}'.format(run_id, attempt)
    if report:
        entry['attempts'][run_key] = {'detected': report['notifications'], 'delivered': report['delivered_notifications']}
    delivered = sum(x['delivered'] for x in entry['attempts'].values())
    errors = report.get('errors', []) if report else []
    completed = bool(outcome == 'success' and report and report.get('complete') is True and not errors
                     and report['notifications'] == report['delivered_notifications'])
    unknown = report.get('stock_unknown', 0) if report else 0
    phase = 'complete_limited' if completed and unknown else 'complete' if completed else 'failure'
    # Same-day reruns stay quiet unless health changes or further updates are delivered.
    signature = json.dumps([phase, delivered, sorted(errors)], ensure_ascii=False)
    should_send = entry.get('signature') != signature
    entry['signature'] = signature
    entry['last_checked_at'] = now.isoformat()
    # Bounded history, independent of both the purchase and watchdog state branches.
    cutoff = (now.astimezone(JST).date() - timedelta(days=30)).isoformat()
    after['days'] = {d: value for d, value in after['days'].items() if d >= cutoff}
    label = '正常終了（在庫判定に一部制限）' if phase == 'complete_limited' else '正常終了' if completed else '要確認・処理エラー'
    lines = ['確認完了: ' + now.astimezone(JST).strftime('%Y/%m/%d %H:%M JST')]
    if completed:
        lines.append('今回の新規情報: {}件'.format(report['notifications']) if report['notifications'] else '今回: 確認できた範囲では新規情報なし')
    else:
        lines.append('処理が正常完了していないため「更新なし」とは判定できません。')
        lines.append('監視ステップ: ' + outcome)
    lines.append('本日①へ送信確認済み: {}件'.format(delivered))
    if report:
        lines.append('取得できた情報源: {}件 / ポケゲト記事: {}件 / トレトク記事: {}件'.format(
            len(report['source_offers']), report.get('catalog_articles', 0), report.get('toretoku_articles', 0)))
        lines.append('Amazon在庫: {}商品中{}商品が判定不能'.format(report['product_urls'], unknown))
        if report['notifications'] > report['delivered_notifications']:
            lines.append('今回未送信: {}件（次回再試行）'.format(report['notifications'] - report['delivered_notifications']))
        if errors:
            lines.append('取得エラー: ' + '、'.join(str(e) for e in errors)[:900])
    else:
        lines.append('今回のチェック結果を取得できません。実行ログをご確認ください。')
    lines.append('ポケセン在庫は直接確認未対応。記事更新だけでは①へ通知しません。')
    record = {'title': day + ' ポケカ日次ヘルスチェック', 'event': label,
              'url': 'https://github.com/{}/actions/runs/{}'.format(repo, run_id),
              'note': '\n'.join(lines), 'source': 'GitHub Actions', 'retailer': '監視システム'}
    return should_send, record, after


def deliver(store, state, report, outcome, now, repo, run_id, attempt, webhook):
    should_send, record, after = plan_health(state, report, outcome, now, repo, run_id, attempt)
    if should_send:
        # Preserve counts across a failed health delivery, without marking notified.
        observed = copy.deepcopy(after)
        day = now.astimezone(JST).date().isoformat()
        observed['days'][day]['signature'] = state['days'].get(day, {}).get('signature')
        if observed != state or store.sha is None:
            store.save(observed)
        send_discord([record], webhook, username='ポケカ ヘルスチェック', content='🩺 ポケカ日次ヘルスチェック')
    # Commit only after Discord confirms receipt; failure remains retryable.
    if after != state or store.sha is None:
        store.save(after)
    print('日次ヘルスチェック: ' + ('②への送信確認済み' if should_send else '本日通知済み・状態変化なし'))
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as file:
            file.write('\n## 日次ヘルスチェック\n\n' + ('②への送信確認済み' if should_send else '本日通知済み・状態変化なし') + '\n\n' + record['note'].replace('\n', '  \n') + '\n')
    return should_send


def main():
    try:
        webhook = os.environ.get('DISCORD_HEALTH_WEBHOOK_URL', '')
        if not webhook:
            raise ValueError('DISCORD_HEALTH_WEBHOOK_URL is missing')
        run_id, attempt = os.environ['GITHUB_RUN_ID'], os.environ.get('GITHUB_RUN_ATTEMPT', '1')
        report = load_report('report.json', run_id, attempt)
        store = HealthStore(GitHubClient())
        deliver(store, store.load(), report, os.environ.get('MONITOR_OUTCOME', 'unknown'),
                datetime.now(JST), os.environ['GITHUB_REPOSITORY'], run_id, attempt, webhook)
        return 0
    except Exception as error:
        print('ヘルスチェック通知失敗: ' + safe_error(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
