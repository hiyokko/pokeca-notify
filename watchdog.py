#!/usr/bin/env python3
"""Bounded recovery for the daily workflow; independent from monitor-state."""
import argparse
import base64
import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode

import re
import time
from urllib.request import Request, urlopen

UTC = timezone.utc
MONITOR_TITLE = 'Pokeca daily monitor'
MAX_RETRIES = 2
COOLDOWN = timedelta(minutes=30)
GRACE = timedelta(hours=2)



UA = "PokecaWatchdog/1.0 (+https://github.com/hiyokko/pokeca-notify)"

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

def safe_error(error):
    # Never log request URLs/headers: they may contain the Discord webhook token.
    if isinstance(error, HTTPError):
        return "HTTP {}".format(error.code)
    return type(error).__name__

def send_discord(records, webhook, *, username="ポケカ watchdog", content="🛠️ ポケカ監視 watchdog"):
    if not re.fullmatch(r"https://(?:canary\.|ptb\.)?discord\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+", webhook):
        raise ValueError("DISCORD_WEBHOOK_URLの形式を確認してください")
    payload = {"username": username, "allowed_mentions": {"parse": []},
               "content": content,
               "embeds": [{"title": ("【" + r.get("event", "購入機会") + "】" + r["title"])[:220],
                           "url": r["url"], "description": r.get("note", "")[:3500],
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

class GitHubClient:
    def __init__(self):
        repo = os.environ['GITHUB_REPOSITORY']
        if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo):
            raise ValueError('Invalid repository')
        self.base = 'https://api.github.com/repos/' + repo
        self.headers = {'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'],
                        'Accept': 'application/vnd.github+json',
                        'X-GitHub-Api-Version': '2022-11-28'}

    def api(self, path, payload=None, method=None):
        text, _ = request(self.base + path, payload, self.headers, method)
        return json.loads(text) if text else {}


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(UTC)


def fresh_watchdog():
    return {'version': 1, 'incident': None, 'last_test_at': None, 'maintenance_sources': []}


def validate_watchdog(state):
    if not isinstance(state, dict) or state.get('version') != 1 or 'incident' not in state:
        raise ValueError('Invalid watchdog state')
    maintenance = state.get('maintenance_sources', [])
    if not isinstance(maintenance, list) or any(not isinstance(x, str) for x in maintenance):
        raise ValueError('Invalid maintenance state')
    incident = state['incident']
    if incident is not None:
        if not isinstance(incident, dict) or not isinstance(incident.get('retries'), int):
            raise ValueError('Invalid recovery budget')
        if not 0 <= incident['retries'] <= MAX_RETRIES:
            raise ValueError('Invalid recovery budget')
        if not isinstance(incident.get('bootstrap_dispatched'), bool):
            raise ValueError('Invalid dispatch state')
    return state


class WatchdogStore:
    branch = 'watchdog-state'

    def __init__(self, client):
        self.client, self.sha = client, None

    def load(self):
        try:
            result = self.client.api('/contents/watchdog.json?ref=' + self.branch)
        except HTTPError as error:
            if error.code != 404:
                raise
            try:
                self.client.api('/git/ref/heads/' + self.branch)
            except HTTPError as branch_error:
                if branch_error.code == 404:
                    return fresh_watchdog()
                raise
            raise ValueError('Existing watchdog branch is missing its state')
        self.sha = result['sha']
        return validate_watchdog(json.loads(base64.b64decode(result['content'])))

    def save(self, state):
        validate_watchdog(state)
        if self.sha is None:
            self.client.api('/git/refs', {'ref': 'refs/heads/' + self.branch,
                                         'sha': os.environ['GITHUB_SHA']})
        payload = {'message': 'Update watchdog recovery state', 'branch': self.branch,
                   'content': base64.b64encode((json.dumps(state, ensure_ascii=False, indent=2) + '\n').encode()).decode()}
        if self.sha:
            payload['sha'] = self.sha
        result = self.client.api('/contents/watchdog.json', payload, 'PUT')
        self.sha = result['content']['sha']


def normal_runs(runs, branch, repository):
    return sorted([r for r in runs if r.get('head_branch') == branch
                   and r.get('head_repository', {}).get('full_name') == repository
                   and r.get('event') in {'schedule', 'workflow_dispatch'}
                   and r.get('display_title') == MONITOR_TITLE],
                  key=lambda r: (r.get('updated_at', r['created_at']), r['id']), reverse=True)


def due_time(now):
    # 09:00 JST = midnight UTC; allow two hours for the GitHub scheduler.
    due = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return due if now >= due + GRACE else due - timedelta(days=1)


def assess(workflow, runs, state, now, current_sha, retry_not_before=None, maintenance_sources=None):
    """Pure decision: changes only happen after a persisted action reservation."""
    incident = state.get('incident')
    if workflow.get('state') != 'active':
        return {'phase': 'attention', 'reason': '日次ワークフローが無効です。設定を確認してください。'}
    running = [r for r in runs if r['status'] != 'completed']
    if running:
        oldest = min(running, key=lambda r: r['created_at'])
        since = max(oldest['created_at'], oldest.get('run_started_at') or oldest['created_at'],
                    oldest.get('updated_at') or oldest['created_at'])
        if now - timestamp(since) > timedelta(hours=1):
            return {'phase': 'attention', 'reason': '日次実行が1時間以上、待機または実行中です。', 'run_id': oldest['id']}
        return {'phase': 'waiting', 'reason': '日次処理が実行中です。'}
    latest = runs[0] if runs else None
    if latest and latest['conclusion'] == 'success' and timestamp(latest.get('updated_at', latest['created_at'])) >= due_time(now):
        if maintenance_sources:
            return {'phase': 'maintenance', 'maintenance_sources': sorted(maintenance_sources),
                    'reason': '公式メンテナンス中: ' + '、'.join(sorted(maintenance_sources)) + '。他の情報源の監視は完了しました。通常の日次チェックで再開を確認します。',
                    'run_id': latest['id']}
        return {'phase': 'healthy', 'reason': '日次チェックは正常です。', 'run_id': latest['id']}
    if incident and incident.get('last_action_at') and now - timestamp(incident['last_action_at']) < COOLDOWN:
        return {'phase': 'waiting', 'reason': '再試行の間隔を確保しています。'}
    if latest and latest['conclusion'] not in {'success', 'failure', 'timed_out', 'startup_failure', 'stale'}:
        return {'phase': 'attention', 'reason': '日次実行が取消・承認待ち等で停止しています。', 'run_id': latest['id']}
    if latest and latest['conclusion'] != 'success':
        if incident and incident['retries'] >= MAX_RETRIES:
            return {'phase': 'attention', 'reason': '自動再試行2回でも復旧しませんでした。', 'run_id': latest['id']}
        if retry_not_before and timestamp(retry_not_before) > now:
            return {'phase': 'waiting', 'reason': '情報源が指定した再取得時刻まで待機します。',
                    'retry_not_before': retry_not_before, 'run_id': latest['id']}
        action = 'rerun' if latest.get('head_sha') == current_sha else 'dispatch'
        return {'phase': 'repairing', 'action': action, 'retry': True,
                'reason': '日次チェックが失敗したため自動再実行します。', 'run_id': latest['id']}
    # No successful normal run for the expected day. Connection tests never count.
    if incident and incident.get('bootstrap_dispatched'):
        return {'phase': 'attention', 'reason': '補完実行を要求済みですが実行を確認できません。'}
    return {'phase': 'repairing', 'action': 'dispatch', 'retry': False,
            'reason': '予定時刻から2時間経過しても日次実行を確認できないため起動します。'}


def reserve(state, decision, now):
    result = copy.deepcopy(state)
    if result['incident'] is None:
        result['incident'] = {'started_at': now.isoformat(), 'retries': 0,
                              'bootstrap_dispatched': False, 'last_notice': None}
    incident = result['incident']
    if decision.get('action'):
        if decision['retry']:
            incident['retries'] += 1
        else:
            incident['bootstrap_dispatched'] = True
        incident['last_action_at'] = now.isoformat()
        incident['last_action'] = decision['action']
        incident['run_id'] = decision.get('run_id')
    return result


def perform(client, decision, branch):
    if decision['action'] == 'rerun':
        client.api('/actions/runs/{}/rerun'.format(int(decision['run_id'])), {}, 'POST')
    else:
        client.api('/actions/workflows/daily.yml/dispatches',
                   {'ref': branch, 'inputs': {'test_notification': 'false'}}, 'POST')


def notice(phase, reason, repo, run_id=None):
    url = 'https://github.com/' + repo + '/actions'
    if run_id:
        url += '/runs/' + str(run_id)
    labels = {'repairing': '自動復旧', 'attention': '要確認', 'healthy': '復旧', 'test': 'テスト', 'maintenance': '一部監視休止'}
    return {'title': 'ポケカ監視 watchdog', 'event': labels[phase], 'url': url,
            'source': 'GitHub Actions watchdog', 'retailer': '監視システム', 'note': reason}


def fetch_runs(client, branch):
    runs = []
    for page in range(1, 11):
        data = client.api('/actions/workflows/daily.yml/runs?' + urlencode({'branch': branch, 'per_page': 100, 'page': page}))
        batch = data['workflow_runs']
        runs.extend(batch)
        if len(batch) < 100:
            break
    return runs


def execute(client, store, state, workflow, runs, now, sha, branch, repo, webhook, dry_run=False, retry_not_before=None, maintenance_sources=None):
    decision = assess(workflow, runs, state, now, sha, retry_not_before, maintenance_sources)
    print(json.dumps(decision, ensure_ascii=False))
    if dry_run:
        return decision
    old = copy.deepcopy(state)
    if decision['phase'] == 'healthy':
        if state['incident'] is not None or state.get('maintenance_sources'):
            if state.get('maintenance_sources') or (state['incident'] and state['incident'].get('last_notice')):
                message = '全情報源の取得成功を確認しました。公式メンテナンスによる監視休止を解除しました。' if state.get('maintenance_sources') else '日次チェックの成功を確認しました。監視が復旧しました。'
                send_discord([notice('healthy', message, repo, decision.get('run_id'))], webhook)
            state['incident'] = None
            state['maintenance_sources'] = []
    elif decision['phase'] == 'maintenance':
        names = decision['maintenance_sources']
        if sorted(state.get('maintenance_sources', [])) != names:
            send_discord([notice('maintenance', decision['reason'], repo, decision.get('run_id'))], webhook)
        state['maintenance_sources'] = names
        # The run completed with verified maintenance warnings and no source errors.
        # End the former failure incident without claiming full source recovery.
        state['incident'] = None
    elif decision['phase'] != 'waiting':
        if decision.get('action'):
            state = reserve(state, decision, now)
            # Persist BEFORE the API call. Ambiguous network failures consume a
            # retry instead of causing unbounded duplicate reruns on the next poll.
            store.save(state)
            perform(client, decision, branch)
        elif state['incident'] is None:
            state = reserve(state, decision, now)
        phase = decision['phase']
        if state['incident'].get('last_notice') != phase:
            send_discord([notice(phase, decision['reason'], repo, decision.get('run_id'))], webhook)
            state['incident']['last_notice'] = phase
    if state != old or store.sha is None:
        store.save(state)
    return decision


def source_context(client, runs):
    if not runs or runs[0].get('conclusion') not in {'success', 'failure', 'timed_out', 'startup_failure', 'stale'}:
        return {}
    try:
        result = client.api('/contents/state.json?ref=monitor-state')
    except HTTPError as error:
        if error.code == 404:
            return {}
        raise
    state = json.loads(base64.b64decode(result['content']))
    report = state.get('last_check', {})
    latest = runs[0]
    if (str(report.get('run_id')) != str(latest['id']) or
            str(report.get('run_attempt')) != str(latest.get('run_attempt', 1))):
        return {}
    value = report.get('retry_not_before')
    if value and datetime.fromisoformat(value).tzinfo is None:
        raise ValueError('Invalid source retry time')
    maintenance = report.get('maintenance_sources', [])
    if not isinstance(maintenance, list) or any(not isinstance(x, str) for x in maintenance):
        raise ValueError('Invalid maintenance report')
    return {'retry_not_before': value, 'maintenance_sources': maintenance}


def source_retry_time(client, runs):
    if not runs or runs[0].get('conclusion') not in {'failure', 'timed_out', 'startup_failure', 'stale'}:
        return None
    return source_context(client, runs).get('retry_not_before')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--test-rerun', action='store_true')
    args = parser.parse_args()
    try:
        client = GitHubClient()
        repo = os.environ['GITHUB_REPOSITORY']
        branch = os.environ.get('DEFAULT_BRANCH', 'main')
        sha = os.environ['GITHUB_SHA']
        webhook = os.environ.get('DISCORD_WEBHOOK_URL', '')
        if not args.dry_run and not webhook:
            raise ValueError('DISCORD_WEBHOOK_URL is missing')
        store = WatchdogStore(client)
        state = store.load()
        workflow = client.api('/actions/workflows/daily.yml')
        runs = normal_runs(fetch_runs(client, branch), branch, repo)
        now = datetime.now(UTC)
        if args.test_rerun:
            if workflow.get('state') != 'active' or not runs or any(r['status'] != 'completed' for r in runs):
                raise ValueError('No idle daily run available for test')
            if state['incident'] is not None:
                raise ValueError('Resolve the active incident before testing')
            if state.get('last_test_at') and now - timestamp(state['last_test_at']) < COOLDOWN:
                raise ValueError('Rerun test cooldown')
            if not args.dry_run:
                state['last_test_at'] = now.isoformat()
                store.save(state)
                perform(client, {'action': 'rerun', 'run_id': runs[0]['id']}, branch)
                send_discord([notice('test', 'watchdogから通常チェックの再実行を要求しました。自動再実行の権限を確認するテストです。', repo, runs[0]['id'])], webhook)
            decision = {'phase': 'test', 'run_id': runs[0]['id']}
        else:
            context = source_context(client, runs)
            decision = execute(client, store, state, workflow, runs, now, sha, branch, repo, webhook, args.dry_run,
                               retry_not_before=context.get('retry_not_before'), maintenance_sources=context.get('maintenance_sources'))
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as f:
                f.write('## Watchdog\n\n```json\n' + json.dumps(decision, ensure_ascii=False, indent=2) + '\n```\n')
        return 0
    except Exception as error:
        print('Watchdog失敗: ' + safe_error(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
