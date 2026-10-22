import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from health import fresh_health, plan_health
from monitor import fresh_state, run
from source_http import SourceFetcher, SourceMaintenance, maintenance_page

NOW = datetime(2026, 10, 23, 0, tzinfo=timezone.utc)
BODY = '<html><head><title>ただいまメンテナンス中です | トレトク【公式】</title></head><body>メンテナンス中</body></html>'


def maintenance_error(body=BODY, code=503, headers=None):
    return HTTPError('https://www.toretoku.jp/blog/wp-json/', code, 'unavailable',
                     headers if headers is not None else {'Retry-After': '3600'}, io.BytesIO(body.encode()))


class MaintenanceDetectionTests(unittest.TestCase):
    def test_requires_503_and_explicit_maintenance_title(self):
        self.assertTrue(maintenance_page(maintenance_error()))
        self.assertFalse(maintenance_page(maintenance_error(code=502)))
        self.assertFalse(maintenance_page(maintenance_error('<title>Service unavailable</title><p>メンテナンス</p>')))
        self.assertFalse(maintenance_page(maintenance_error('<h1>ただいまメンテナンス中です</h1>')))

    def test_maintenance_saved_without_inline_retry(self):
        request = Mock(side_effect=maintenance_error())
        sleep = Mock()
        client = SourceFetcher(request, clock=lambda: NOW, sleep=sleep)
        with self.assertRaises(SourceMaintenance) as result:
            client.get('https://www.toretoku.jp/blog/wp-json/')
        self.assertEqual(result.exception.retry_at, (NOW + timedelta(hours=1)).isoformat())
        self.assertEqual(client.maintenance_hosts, {'www.toretoku.jp'})
        self.assertEqual(request.call_count, 1)
        self.assertEqual(client.stats['www.toretoku.jp']['failed_requests'], 0)
        sleep.assert_not_called()

    def test_known_maintenance_preserved_during_cooldown_then_cleared(self):
        deadline = (NOW + timedelta(hours=1)).isoformat()
        request = Mock(return_value=('[]', {}))
        clock = Mock(return_value=NOW)
        client = SourceFetcher(request, {'www.toretoku.jp': deadline}, maintenance_hosts=['www.toretoku.jp'], clock=clock)
        with self.assertRaises(SourceMaintenance):
            client.get('https://www.toretoku.jp/blog/wp-json/')
        request.assert_not_called()
        clock.return_value = NOW + timedelta(hours=1)
        self.assertEqual(client.json('https://www.toretoku.jp/blog/wp-json/')[0], [])
        self.assertEqual(client.maintenance_hosts, set())
        self.assertEqual(client.retry_after, {})

    def test_missing_retry_after_defaults_to_one_hour(self):
        client = SourceFetcher(Mock(side_effect=maintenance_error(headers={})), clock=lambda: NOW)
        with self.assertRaises(SourceMaintenance) as result:
            client.get('https://www.toretoku.jp/blog/wp-json/')
        self.assertEqual(result.exception.retry_at, (NOW + timedelta(hours=1)).isoformat())

    def test_plain_503_remains_failure(self):
        request = Mock(side_effect=maintenance_error('<title>Service Unavailable</title>'))
        client = SourceFetcher(request, clock=lambda: NOW, sleep=Mock())
        with self.assertRaises(HTTPError):
            client.get('https://www.toretoku.jp/blog/wp-json/')
        self.assertEqual(client.maintenance_hosts, set())


class MaintenanceReportingTests(unittest.TestCase):
    @patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': ''})
    def check(self, index_failure=False):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as folder:
            try:
                os.chdir(folder)
                Path('config.json').write_text(json.dumps({'check_toretoku_psa_oripa': True}))
                state = fresh_state()
                state['posts']['1'] = {'title': 'テストBOX', 'kind': 'box', 'modified': 'x', 'offers': []}
                store = Mock(); store.load.return_value = state
                index = Mock(return_value=[{'id': 1, 'modified_gmt': 'x'}])
                if index_failure:
                    index.side_effect = HTTPError('https://source.example/', 502, 'bad gateway', {}, None)
                args = SimpleNamespace(state='unused', github_state=False, dry_run=True, test_notification=False)
                with patch('monitor.StateStore', return_value=store), patch('monitor.post_index', index), patch('monitor.request', side_effect=lambda _: (_ for _ in ()).throw(maintenance_error())), patch('monitor.time.sleep'), patch('monitor.send_discord') as send, patch('builtins.print'):
                    code = run(args)
                    send.assert_not_called()
                result = json.loads(Path('report.json').read_text())
                return code, result, state
            finally:
                os.chdir(original)

    def test_other_sources_finish_without_error_or_fake_all_clear(self):
        code, report, state = self.check()
        self.assertEqual(code, 0)
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['maintenance_sources'], ['トレトク公式ブログ'])
        self.assertEqual(report['source_status']['ポケゲトちゃんねる']['status'], 'ok')
        self.assertEqual(report['source_status']['トレトク公式ブログ']['status'], 'maintenance')
        self.assertEqual(state['source_maintenance'], ['www.toretoku.jp'])
        self.assertEqual(state['last_check']['maintenance_sources'], ['トレトク公式ブログ'])
        _, record, _ = plan_health(fresh_health(), report, 'success', NOW, 'owner/repo', '1', '1')
        self.assertIn('一部監視休止', record['event'])
        self.assertIn('全体の「更新なし」は未確認', record['note'])
        self.assertIn('通常の日次チェックで再開', record['note'])
        self.assertNotIn('正常終了', record['event'])
        self.assertNotIn('新規情報なし', record['note'])

    def test_maintenance_never_hides_unrelated_source_error(self):
        code, report, _ = self.check(index_failure=True)
        self.assertEqual(code, 1)
        self.assertEqual(report['errors'], ['ポケゲト記事一覧: HTTP 502'])
        self.assertEqual(len(report['warnings']), 1)
        _, record, _ = plan_health(fresh_health(), report, 'failure', NOW, 'owner/repo', '1', '1')
        self.assertIn('要確認', record['event'])
        self.assertNotIn('正常終了', record['event'])


if __name__ == '__main__':
    unittest.main()
