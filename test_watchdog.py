import copy
import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from watchdog import (UTC, MONITOR_TITLE, WatchdogStore, GitHubClient, assess, due_time, execute,
                      fresh_watchdog, normal_runs, perform, reserve, validate_watchdog)

NOW = datetime(2026, 10, 16, 2, 17, tzinfo=UTC)  # 11:17 JST
REPO = 'owner/repo'
ACTIVE = {'state': 'active'}


def run(**changes):
    data = dict(id=1, display_title=MONITOR_TITLE, event='schedule', head_branch='main',
                head_repository={'full_name': REPO}, created_at='2026-10-16T00:00:00Z',
                updated_at='2026-10-16T00:05:00Z', run_started_at='2026-10-16T00:00:00Z',
                status='completed', conclusion='success', head_sha='current', run_attempt=1)
    data.update(changes)
    return data


def decision(runs=None, state=None, now=NOW, workflow=None):
    return assess(workflow or ACTIVE, runs or [], state or fresh_watchdog(), now, 'current')


class RecoveryDecisionTests(unittest.TestCase):
    def test_healthy_quiet(self):
        self.assertEqual(decision([run()])['phase'], 'healthy')

    def test_failure_and_timeout_are_rerun(self):
        for conclusion in ('failure', 'timed_out', 'startup_failure', 'stale'):
            result = decision([run(conclusion=conclusion)])
            self.assertEqual(result['action'], 'rerun')
            self.assertTrue(result['retry'])

    def test_new_code_dispatches_current_branch_instead_of_old_commit(self):
        self.assertEqual(decision([run(conclusion='failure', head_sha='outdated')])['action'], 'dispatch')

    def test_failure_budget_is_persistent_and_exhausts_at_two(self):
        state = fresh_watchdog()
        bad = [run(conclusion='failure')]
        first = decision(bad, state)
        state = reserve(state, first, NOW)
        self.assertEqual(state['incident']['retries'], 1)
        self.assertEqual(decision(bad, state)['phase'], 'waiting')
        later = NOW + timedelta(minutes=30)
        second = decision(bad, state, later)
        state = reserve(state, second, later)
        self.assertEqual(state['incident']['retries'], 2)
        self.assertEqual(decision(bad, state, later + timedelta(minutes=30))['phase'], 'attention')

    def test_missed_run_dispatch_and_two_retries(self):
        state = fresh_watchdog()
        missing = decision([], state)
        self.assertEqual(missing['action'], 'dispatch')
        self.assertFalse(missing['retry'])
        state = reserve(state, missing, NOW)
        self.assertEqual(state['incident']['retries'], 0)
        self.assertEqual(decision([], state, NOW + timedelta(minutes=30))['phase'], 'attention')
        self.assertEqual(decision([run(conclusion='failure')], state, NOW + timedelta(minutes=30))['action'], 'rerun')

    def test_schedule_grace_uses_japan_time(self):
        before = datetime(2026, 10, 16, 1, 59, tzinfo=UTC)
        self.assertEqual(due_time(before).day, 15)
        self.assertEqual(due_time(NOW).day, 16)
        yesterday = run(created_at='2026-10-15T00:00:00Z', updated_at='2026-10-15T00:05:00Z')
        self.assertEqual(decision([yesterday], now=before)['phase'], 'healthy')
        self.assertEqual(decision([yesterday])['action'], 'dispatch')

    def test_connection_tests_and_other_branches_do_not_mask_missing_run(self):
        values = [run(display_title='Discord connection test'), run(head_branch='test'),
                  run(head_repository={'full_name': 'attacker/fork'}), run(event='pull_request')]
        self.assertEqual(normal_runs(values, 'main', REPO), [])

    def test_current_running_job_is_not_duplicated(self):
        pending = run(status='queued', conclusion=None, updated_at=NOW.isoformat())
        self.assertEqual(decision([pending])['phase'], 'waiting')

    def test_stuck_run_needs_attention_not_duplicate_work(self):
        self.assertEqual(decision([run(status='in_progress', conclusion=None)])['phase'], 'attention')

    def test_successful_rerun_of_old_run_counts_as_fresh_success(self):
        recovered = run(created_at='2026-10-15T00:00:00Z', updated_at=NOW.isoformat(), run_attempt=2)
        self.assertEqual(decision([recovered])['phase'], 'healthy')

    def test_latest_completed_attempt_takes_precedence(self):
        old_recovered = run(id=1, created_at='2026-10-15T00:00:00Z', updated_at=NOW.isoformat())
        newer_failed = run(id=2, conclusion='failure')
        values = normal_runs([newer_failed, old_recovered], 'main', REPO)
        self.assertEqual(values[0]['id'], 1)

    def test_disabled_and_cancelled_are_not_automatically_overridden(self):
        self.assertNotIn('action', decision([run()], workflow={'state':'disabled_manually'}))
        self.assertNotIn('action', decision([run(conclusion='cancelled')]))

    def test_corrupt_retry_state_does_not_reset_budget(self):
        with self.assertRaises(ValueError): validate_watchdog({'version': 1, 'incident': {'retries': '0'}})


class DeliveryAndApiTests(unittest.TestCase):
    def setUp(self):
        self.client, self.store = Mock(), Mock()
        self.store.sha = 'existing'

    def call(self, state, runs, now=NOW, dry_run=False):
        return execute(self.client, self.store, state, ACTIVE, runs, now, 'current',
                       'main', REPO, 'fake-webhook', dry_run)

    @patch('watchdog.send_discord')
    def test_notice_once_during_repairs_then_once_on_recovery(self, send):
        state = fresh_watchdog()
        snapshots = []
        self.store.save.side_effect = lambda s: snapshots.append(copy.deepcopy(s))
        self.call(state, [run(conclusion='failure')])
        self.assertEqual(send.call_count, 1)
        state = snapshots[-1]
        self.call(state, [run(conclusion='failure')], NOW + timedelta(minutes=30))
        self.assertEqual(send.call_count, 1)
        state = snapshots[-1]
        self.call(state, [run(updated_at=(NOW + timedelta(minutes=32)).isoformat())], NOW + timedelta(minutes=33))
        self.assertEqual(send.call_count, 2)
        self.assertIsNone(snapshots[-1]['incident'])
        self.call(snapshots[-1], [run()], NOW + timedelta(minutes=34))
        self.assertEqual(send.call_count, 2)

    @patch('watchdog.send_discord')
    def test_failed_api_call_still_consumes_reserved_retry(self, send):
        snapshots = []
        self.store.save.side_effect = lambda s: snapshots.append(copy.deepcopy(s))
        self.client.api.side_effect = TimeoutError('ambiguous response')
        with self.assertRaises(TimeoutError):self.call(fresh_watchdog(), [run(conclusion='failure')])
        self.assertEqual(snapshots[0]['incident']['retries'], 1)
        send.assert_not_called()

    @patch('watchdog.send_discord')
    def test_failed_discord_delivery_is_not_marked_notified(self, send):
        send.side_effect = ValueError('no delivery confirmation')
        snapshots = []
        self.store.save.side_effect = lambda s: snapshots.append(copy.deepcopy(s))
        with self.assertRaises(ValueError):self.call(fresh_watchdog(), [run(conclusion='failure')])
        self.assertIsNone(snapshots[-1]['incident']['last_notice'])

    @patch('watchdog.send_discord')
    def test_dry_run_has_no_side_effects(self, send):
        self.call(fresh_watchdog(), [run(conclusion='failure')], dry_run=True)
        self.client.api.assert_not_called()
        self.store.save.assert_not_called()
        send.assert_not_called()

    def test_dispatch_normal_monitor_and_rerun_exact_run(self):
        perform(self.client, {'action':'dispatch'}, 'main')
        self.assertEqual(self.client.api.call_args.args[1], {'ref':'main', 'inputs':{'test_notification':'false'}})
        perform(self.client, {'action':'rerun','run_id':123}, 'main')
        self.assertEqual(self.client.api.call_args.args[0], '/actions/runs/123/rerun')

    @patch.dict(os.environ, {'GITHUB_REPOSITORY':'owner/repo','GITHUB_TOKEN':'fake'})
    @patch('watchdog.request', return_value=('', {}))
    def test_github_204_success_is_supported(self, request):
        self.assertEqual(GitHubClient().api('/actions/workflows/daily.yml/dispatches', {}, 'POST'), {})

    def test_existing_watchdog_branch_with_missing_file_is_not_reset(self):
        error = HTTPError('https://api.github.com/',404,'missing',{},None)
        self.client.api.side_effect = [error, {'ref':'heads/watchdog-state'}]
        with self.assertRaises(ValueError):WatchdogStore(self.client).load()


class MaintenanceRecoveryTests(unittest.TestCase):
    def test_successful_partial_maintenance_is_not_total_recovery(self):
        result = assess(ACTIVE, [run()], fresh_watchdog(), NOW, 'current', maintenance_sources=['トレトク'])
        self.assertEqual(result['phase'], 'maintenance')
        self.assertNotIn('action', result)
        self.assertIn('通常の日次チェック', result['reason'])

    @patch('watchdog.send_discord')
    def test_old_exhausted_incident_becomes_maintenance_notice_once(self, send):
        client, store = Mock(), Mock(sha='existing')
        state = fresh_watchdog()
        state['incident'] = {'retries': 2, 'bootstrap_dispatched': False, 'last_notice': 'attention'}
        args = (client, store, state, ACTIVE, [run()], NOW, 'current', 'main', REPO, 'health-webhook')
        result = execute(*args, maintenance_sources=['トレトク'])
        self.assertEqual(result['phase'], 'maintenance')
        self.assertIsNone(state['incident'])
        self.assertEqual(send.call_args.args[0][0]['event'], '一部監視休止')
        execute(*args, maintenance_sources=['トレトク'])
        self.assertEqual(send.call_count, 1)
        client.api.assert_not_called()

    @patch('watchdog.send_discord')
    def test_real_source_recovery_after_maintenance_notified_once(self, send):
        client, store = Mock(), Mock(sha='existing')
        state = fresh_watchdog(); state['maintenance_sources'] = ['トレトク']
        args = (client, store, state, ACTIVE, [run()], NOW, 'current', 'main', REPO, 'health-webhook')
        execute(*args)
        self.assertEqual(send.call_args.args[0][0]['event'], '復旧')
        self.assertEqual(state['maintenance_sources'], [])
        execute(*args)
        self.assertEqual(send.call_count, 1)

    def test_actual_failure_still_uses_bounded_retries(self):
        state = fresh_watchdog()
        state['incident'] = {'retries': 2, 'bootstrap_dispatched': False, 'last_notice': 'attention'}
        result = assess(ACTIVE, [run(conclusion='failure')], state, NOW, 'current', maintenance_sources=['トレトク'])
        self.assertEqual(result['phase'], 'attention')
        self.assertNotIn('action', result)

    @patch('watchdog.send_discord', side_effect=TimeoutError())
    def test_maintenance_notice_failure_does_not_clear_incident(self, send):
        client, store = Mock(), Mock(sha='existing')
        state = fresh_watchdog()
        state['incident'] = {'retries': 2, 'bootstrap_dispatched': False, 'last_notice': 'attention'}
        with self.assertRaises(TimeoutError):
            execute(client, store, state, ACTIVE, [run()], NOW, 'current', 'main', REPO, 'health-webhook', maintenance_sources=['トレトク'])
        self.assertIsNotNone(state['incident'])
        store.save.assert_not_called()

if __name__ == '__main__':
    unittest.main()
