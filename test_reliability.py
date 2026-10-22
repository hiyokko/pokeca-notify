import base64
import copy
import json
import os
import ssl
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from events import JST, observation
from health import fresh_health, plan_health
from monitor import fresh_state, known_product_urls, run
from source_http import SourceDeferred, SourceFetcher, retry_seconds
from watchdog import assess, fresh_watchdog, source_retry_time

NOW = datetime(2026, 10, 22, 3, 0, tzinfo=timezone.utc)


def error(code, retry=None):
    return HTTPError('https://source.example/api', code, 'temporary',
                     {'Retry-After': str(retry)} if retry is not None else {}, None)


class SourceRetryTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.sleeps = []
        self.request = Mock()
        self.client = SourceFetcher(self.request, clock=lambda: self.now, sleep=self.sleep, jitter=lambda: 0)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)

    def test_502_503_recover_before_monitor_failure(self):
        self.request.side_effect = [error(502), error(503), ('{"ok":true}', {})]
        self.assertEqual(self.client.json('https://source.example/api')[0], {'ok': True})
        self.assertEqual(self.sleeps, [2, 5])
        self.assertEqual(self.client.stats['source.example']['recovered_requests'], 1)
        self.assertEqual(self.client.retry_after, {})

    def test_short_retry_after_respected(self):
        self.request.side_effect = [error(429, 10), ('ok', {})]
        self.client.get('https://source.example/api')
        self.assertEqual(self.sleeps, [10])

    def test_hour_retry_after_persisted_and_restored(self):
        self.request.side_effect = error(503, 3600)
        with self.assertRaises(HTTPError):
            self.client.get('https://source.example/api')
        self.assertEqual(self.request.call_count, 1)
        self.assertEqual(self.sleeps, [])
        another = SourceFetcher(self.request, self.client.retry_after, clock=lambda: self.now)
        with self.assertRaises(SourceDeferred):
            another.get('https://source.example/other-endpoint')
        self.assertEqual(self.request.call_count, 1)
        self.now += timedelta(hours=1)
        self.request.side_effect = None
        self.request.return_value = ('ok', {})
        self.assertEqual(another.get('https://source.example/api')[0], 'ok')

    def test_exhausted_host_stops_other_endpoints(self):
        self.request.side_effect = error(502)
        with self.assertRaises(HTTPError):
            self.client.get('https://source.example/api')
        self.assertEqual(self.request.call_count, 4)
        self.assertEqual(self.sleeps, [2, 5, 15])
        with self.assertRaises(SourceDeferred):
            self.client.get('https://source.example/another')
        self.assertEqual(self.request.call_count, 4)
        self.assertEqual(self.client.stats['source.example']['deferred_requests'], 1)

    def test_permanent_http_and_tls_errors_not_retried(self):
        for failure in (error(403), error(404), URLError(ssl.SSLCertVerificationError('invalid'))):
            with self.subTest(failure=type(failure).__name__):
                request = Mock(side_effect=failure)
                client = SourceFetcher(request, clock=lambda: NOW, sleep=Mock())
                with self.assertRaises(type(failure)):
                    client.get('https://source.example/api')
                self.assertEqual(request.call_count, 1)

    def test_connection_timeout_retried(self):
        self.request.side_effect = [URLError(TimeoutError()), ('ok', {})]
        self.assertEqual(self.client.get('https://source.example/api')[0], 'ok')
        self.assertEqual(self.sleeps, [2])

    def test_retry_wait_budget_is_bounded_across_hosts(self):
        self.request.side_effect = error(503, 30)
        with self.assertRaises(HTTPError):
            self.client.get('https://first.example/api')
        with self.assertRaises(HTTPError):
            self.client.get('https://second.example/api')
        self.assertEqual(sum(self.sleeps), 90)
        self.assertEqual(self.request.call_count, 5)

    def test_retry_after_http_date_and_invalid_value(self):
        self.assertEqual(retry_seconds(format_datetime(NOW + timedelta(minutes=1)), NOW), 60)
        self.assertIsNone(retry_seconds('not-a-date', NOW))
        self.assertEqual(retry_seconds(format_datetime(NOW - timedelta(minutes=1)), NOW), 0)

    def test_parser_failure_not_a_network_retry(self):
        self.request.return_value = ('not JSON', {})
        with self.assertRaises(ValueError):
            self.client.json('https://source.example/api')
        self.assertEqual(self.request.call_count, 1)

    def test_non_https_and_credential_urls_rejected(self):
        for url in ['http://source.example/api', 'https://user:password@source.example/api']:
            with self.assertRaises(ValueError):
                self.client.get(url)
        self.request.assert_not_called()


class CachedDiscoveryTests(unittest.TestCase):
    @patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': ''})
    def test_failed_index_keeps_amazon_targets_without_old_lottery_votes(self):
        state = fresh_state()
        old = observation('テスト BOX', 'Amazon', 'lottery', 'ポケゲトちゃんねる', 'https://pokemon-infomation.com/p',
                          'https://www.amazon.co.jp/dp/B012345678', 'open')
        state['posts']['1'] = {'title': 'テスト BOX', 'kind': 'box', 'offers': [old], 'modified': 'old'}
        state['source_status'] = {'ポケゲトちゃんねる': {'last_success_at': '2026-10-21T09:00:00+09:00'}}
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as folder:
            try:
                os.chdir(folder)
                Path('config.json').write_text(json.dumps({'check_amazon_stock': True, 'check_toretoku_psa_oripa': True}))
                store = Mock(); store.load.return_value = state
                args = SimpleNamespace(state='unused', github_state=False, dry_run=True, test_notification=False)
                with patch('monitor.StateStore', return_value=store), patch('monitor.post_index', side_effect=error(502)), patch('monitor.toretoku_posts', side_effect=error(503)), patch('monitor.request', return_value=('', {})) as request, patch('monitor.time.sleep'), patch('monitor.plan_events', return_value=[]) as planner, patch('builtins.print'):
                    self.assertEqual(run(args), 1)
                    self.assertEqual(request.call_count, 1)
                    self.assertEqual(planner.call_args.args[1], [])
                result = json.loads(Path('report.json').read_text())
                self.assertEqual(result['product_urls'], 1)
                self.assertEqual(result['source_offers'], {})
                self.assertEqual(result['source_status']['ポケゲトちゃんねる']['articles'], 0)
                self.assertEqual(result['source_status']['ポケゲトちゃんねる']['cached_articles'], 1)
                self.assertEqual(result['source_status']['ポケゲトちゃんねる']['last_success_at'], '2026-10-21T09:00:00+09:00')
                store.save.assert_not_called()
            finally:
                os.chdir(original)

    def test_product_discovery_survives_summary_source_outage(self):
        state = fresh_state()
        saved = observation('保存済み BOX', 'Amazon', 'lottery', 'summary', 'https://source.example/',
                            'https://www.amazon.co.jp/dp/B012345678', 'open')
        state['product_catalog'] = {saved['url']: saved}
        self.assertEqual(list(known_product_urls(state, [])), [saved['url']])

    def test_cached_excluded_product_is_not_discovered(self):
        state = fresh_state()
        state['posts']['1'] = {'kind': 'excluded', 'offers': [{'url': 'https://www.amazon.co.jp/dp/B012345678'}]}
        self.assertEqual(known_product_urls(state, []), {})


class DeferredRecoveryTests(unittest.TestCase):
    def failed_run(self):
        return {'id': 123, 'run_attempt': 2, 'created_at': NOW.isoformat(), 'updated_at': NOW.isoformat(),
                'status': 'completed', 'conclusion': 'failure', 'head_sha': 'current'}

    def test_watchdog_waits_without_spending_retry_then_resumes(self):
        state = fresh_watchdog()
        until = (NOW + timedelta(hours=1)).isoformat()
        snapshot = copy.deepcopy(state)
        result = assess({'state': 'active'}, [self.failed_run()], state, NOW, 'current', until)
        self.assertEqual(result['phase'], 'waiting')
        self.assertNotIn('action', result)
        self.assertEqual(state, snapshot)
        result = assess({'state': 'active'}, [self.failed_run()], state, NOW + timedelta(hours=1), 'current', until)
        self.assertEqual(result['action'], 'rerun')

    def test_retry_limit_is_reported_even_during_source_maintenance(self):
        state = fresh_watchdog()
        state['incident'] = {'retries': 2, 'last_action_at': (NOW - timedelta(hours=1)).isoformat()}
        result = assess({'state': 'active'}, [self.failed_run()], state, NOW, 'current', (NOW + timedelta(hours=1)).isoformat())
        self.assertEqual(result['phase'], 'attention')
        self.assertNotIn('action', result)

    def test_old_attempt_retry_hint_cannot_delay_new_failure(self):
        def client(attempt):
            body = {'last_check': {'run_id': '123', 'run_attempt': str(attempt), 'retry_not_before': (NOW + timedelta(hours=1)).isoformat()}}
            return Mock(api=Mock(return_value={'content': base64.b64encode(json.dumps(body).encode()).decode()}))
        self.assertIsNone(source_retry_time(client(1), [self.failed_run()]))
        self.assertIsNotNone(source_retry_time(client(2), [self.failed_run()]))

    def test_legacy_monitor_state_without_report_is_supported(self):
        client = Mock(api=Mock(return_value={'content': base64.b64encode(b'{}').decode()}))
        self.assertIsNone(source_retry_time(client, [self.failed_run()]))

    def test_healthy_run_never_reads_retry_hint(self):
        run = self.failed_run(); run['conclusion'] = 'success'
        client = Mock()
        self.assertIsNone(source_retry_time(client, [run]))
        client.api.assert_not_called()


class HealthAccuracyTests(unittest.TestCase):
    def test_failed_sources_not_counted_as_successful_or_fresh(self):
        report = {'complete': True, 'notifications': 0, 'delivered_notifications': 0, 'stock_unknown': 6,
                  'product_urls': 10, 'errors': ['ポケゲト: HTTP 502', 'トレトク: HTTP 503'],
                  'source_offers': {'Mirar': 15}, 'catalog_articles': 67, 'toretoku_articles': 0,
                  'source_status': {'Mirar': {'status': 'ok'}, 'ポケゲトちゃんねる': {'status': 'failed', 'articles': 0}, 'トレトク公式ブログ': {'status': 'failed', 'articles': 0}},
                  'retry_not_before': (NOW + timedelta(hours=1)).isoformat(),
                  'http_retries': {'pokemon-infomation.com': {'retries': 3, 'recovered_requests': 0}}}
        _, record, _ = plan_health(fresh_health(), report, 'failure', NOW, 'owner/repo', '123', '1')
        self.assertIn('要確認・情報源の取得失敗', record['event'])
        self.assertIn('1 / 3情報源', record['note'])
        self.assertIn('今回0記事を確認 / 保存済み67記事', record['note'])
        self.assertIn('再取得可能時刻: 10/22 13:00 JST以降', record['note'])
        self.assertNotIn('新規情報なし', record['note'])
        self.assertIn('HTTP再試行: 3回', record['note'])


if __name__ == '__main__':
    unittest.main()
