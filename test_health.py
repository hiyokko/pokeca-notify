import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from health import JST, HealthStore, deliver, fresh_health, load_report, plan_health
from monitor import fresh_state, run

NOW = datetime(2026, 10, 18, 9, 1, tzinfo=JST)


def report(**updates):
    return dict({'run_id': '1', 'run_attempt': '1', 'complete': True,
                 'notifications': 0, 'delivered_notifications': 0, 'errors': [],
                 'source_offers': {'ポケゲト': 28, 'トレトク': 15}, 'catalog_articles': 67,
                 'toretoku_articles': 33, 'product_urls': 10, 'stock_unknown': 0}, **updates)


def plan(state=None, result=None, outcome='success', now=NOW, attempt='1'):
    return plan_health(state or fresh_health(), report() if result is None else result,
                       outcome, now, 'owner/repo', '1', attempt)


class HealthTests(unittest.TestCase):
    def test_daily_no_changes_and_next_day(self):
        send, message, state = plan()
        self.assertTrue(send)
        self.assertIn('新規情報なし', message['note'])
        self.assertIn('正常終了', message['event'])
        self.assertFalse(plan(state, now=NOW + timedelta(minutes=5))[0])
        self.assertTrue(plan(state, now=NOW + timedelta(days=1))[0])

    def test_stock_unknown_is_explicit(self):
        _, message, _ = plan(result=report(stock_unknown=8))
        self.assertIn('一部制限', message['event'])
        self.assertIn('10商品中8商品が判定不能', message['note'])
        self.assertIn('確認できた範囲', message['note'])

    def test_errors_never_claim_no_updates(self):
        for outcome in ['failure', 'skipped', 'cancelled', 'success']:
            _, message, _ = plan(result=report(errors=['トレトク: HTTP 503']), outcome=outcome)
            self.assertIn('要確認', message['event'])
            self.assertNotIn('新規情報なし', message['note'])

    def test_missing_and_incomplete_report_are_failures(self):
        for result in [None, report(complete=False)]:
            _, message, _ = plan_health(fresh_health(), result, 'success', NOW, 'owner/repo', '1', '1')
            self.assertIn('要確認', message['event'])

    def test_recovery_sends_once(self):
        _, _, state = plan(result=report(errors=['HTTP 503']), outcome='failure')
        send, _, state = plan(state, attempt='2')
        self.assertTrue(send)
        self.assertFalse(plan(state, attempt='3')[0])

    def test_rerun_retains_daily_delivery_count(self):
        _, _, state = plan(result=report(notifications=2, delivered_notifications=2))
        send, message, state = plan(state, attempt='2')
        self.assertFalse(send)
        self.assertIn('本日①へ送信確認済み: 2件', message['note'])
        send, message, _ = plan(state, result=report(notifications=1, delivered_notifications=1), attempt='3')
        self.assertTrue(send)
        self.assertIn('本日①へ送信確認済み: 3件', message['note'])

    def test_partial_delivery_and_same_attempt_not_double_counted(self):
        result = report(notifications=7, delivered_notifications=5, complete=False)
        _, message, state = plan(result=result, outcome='failure')
        self.assertIn('今回未送信: 2件', message['note'])
        send, message, _ = plan(state, result=result, outcome='failure')
        self.assertFalse(send)
        self.assertIn('本日①へ送信確認済み: 5件', message['note'])

    def test_jst_date_boundary(self):
        _, message, _ = plan(now=datetime.fromisoformat('2026-10-18T15:01:00+00:00'))
        self.assertIn('2026-10-19', message['title'])

    def test_old_or_broken_report_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'report.json'
            path.write_text(json.dumps(report()))
            self.assertIsNotNone(load_report(path, '1', '1'))
            self.assertIsNone(load_report(path, '2', '1'))
            self.assertIsNone(load_report(path, '1', '2'))
            path.write_text('{}')
            self.assertIsNone(load_report(path, '1', '1'))

    @patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': ''})
    @patch('health.send_discord')
    def test_only_channel_two_and_commit_after_receipt(self, send):
        store = Mock(sha='abc')
        deliver(store, fresh_health(), report(), 'success', NOW, 'owner/repo', '1', '1', 'channel-two')
        self.assertEqual(send.call_args.args[1], 'channel-two')
        self.assertEqual(send.call_args.kwargs['username'], 'ポケカ ヘルスチェック')
        self.assertEqual(store.save.call_count, 2)
        send.side_effect = TimeoutError()
        store.reset_mock()
        with self.assertRaises(TimeoutError):
            deliver(store, fresh_health(), report(), 'success', NOW, 'owner/repo', '1', '1', 'channel-two')
        store.save.assert_called_once()
        self.assertIsNone(store.save.call_args.args[0]['days']['2026-10-18']['signature'])

    @patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': ''})
    @patch('health.send_discord', side_effect=TimeoutError())
    def test_failed_health_delivery_retains_product_counts(self, send):
        store = Mock(sha='abc')
        with self.assertRaises(TimeoutError):
            deliver(store, fresh_health(), report(notifications=2, delivered_notifications=2), 'success', NOW, 'owner/repo', '1', '1', 'channel-two')
        observed = store.save.call_args.args[0]
        should_send, message, _ = plan(observed, attempt='2')
        self.assertTrue(should_send)
        self.assertIn('本日①へ送信確認済み: 2件', message['note'])

    def test_missing_state_on_existing_branch_not_reset(self):
        client = Mock()
        client.api.side_effect = [HTTPError('https://api.github.com/', 404, '', {}, None), {'ref': 'existing'}]
        with self.assertRaises(ValueError):
            HealthStore(client).load()


class MonitorReportingTests(unittest.TestCase):
    @patch.dict(os.environ, {'DISCORD_WEBHOOK_URL': 'channel-one', 'GITHUB_RUN_ID': '7', 'GITHUB_RUN_ATTEMPT': '2'})
    def test_progress_reports_delivery_before_state_save_failure(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as folder:
            try:
                os.chdir(folder)
                Path('config.json').write_text('{}')
                state = fresh_state()
                state['posts']['1'] = {'title': 'test BOX', 'kind': 'box', 'offers': [], 'modified': 'x'}
                store = Mock()
                store.load.return_value = state
                store.save.side_effect = [None, TimeoutError()]
                record = {'key': 'one', 'after': {}, 'event': '抽選開始', 'retailer': 'Amazon', 'title': 'test'}
                args = SimpleNamespace(state='unused', github_state=False, dry_run=False, test_notification=False)
                with patch('monitor.StateStore', return_value=store), patch('monitor.post_index', return_value=[{'id': 1, 'modified_gmt': 'x'}]), patch('monitor.plan_events', return_value=[record]), patch('monitor.send_discord') as send:
                    with self.assertRaises(TimeoutError):
                        run(args)
                    self.assertEqual(send.call_args.args[1], 'channel-one')
                result = load_report('report.json', '7', '2')
                self.assertFalse(result['complete'])
                self.assertEqual(result['delivered_notifications'], 1)
            finally:
                os.chdir(original)


if __name__ == '__main__':
    unittest.main()
