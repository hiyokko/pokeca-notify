import copy
import unittest
from datetime import datetime
from unittest.mock import Mock

from events import JST, at_time, pokeget_offers, plan_events
from health import fresh_health, plan_health
from monitor import fresh_state, legacy_catalog, parser_refresh_ids
from pco import ROOT, SOURCE, application_offer, fetch_offers, official_url

NAME = '拡張パック『ハドウシーカー』'
PRODUCT = {'title': NAME, 'kind': 'box', 'release_year': 2026}
FIRST_URL = ROOT + '--6ad759ab34282d555d428fd7'
SECOND_URL = ROOT + '--6ae7f77f0958c211cfae7b57'


def time(value):
    return datetime.fromisoformat(value + '+09:00')


def ready():
    state = fresh_state()
    state['started_at'] = '2026-10-16T00:00:00'
    state['initialized_sources'] = ['ポケゲトちゃんねる']
    return state


def table(rows):
    return '<table>' + ''.join('<tr><td>%s</td><td>%s</td></tr>' % pair for pair in rows) + '</table>'


def summary(period='10月26日(月)16時59分まで', round_name='1回目'):
    content = table([('商品名', NAME), ('発売日', '2026年11月27日'), ('定価', '1BOX 6,000円')])
    content += '<h3>ポケモンセンターオンラインの抽選情報</h3><h4>' + round_name + '</h4>'
    content += table([('販売形式', '抽選販売'), ('受付期間', period),
                      ('購入期間', '11月17日(火)15時〜11月24日(火)16時59分')])
    return {'link': 'https://pokemon-infomation.com/latest-information-hadou/',
            'title': {'rendered': NAME}, 'content': {'rendered': content}}


def official(period='10月23日(金)16時00分〜10月26日(月)16時59分'):
    return '<h1 class="title">【応募受付】ポケモンカードゲーム MEGA 拡張パック「ハドウシーカー」BOXの抽選販売</h1>' + table([
        ('応募受付期間', period), ('抽選結果発表日', '10月30日(金)15時以降'),
        ('注文期間', '10月30日(金)15時〜11月2日(月)16時59分')])


def commit(state, records):
    for record in records:
        state['events'][record['key']] = record['after']


class WindowRegressionTests(unittest.TestCase):
    def test_oct24_deadline_only_not_silently_ignored(self):
        found, _ = pokeget_offers(summary())
        self.assertIsNone(found[0]['start'])
        self.assertTrue(found[0]['application_deadline'])
        state = ready()
        records = plan_events(state, found, time('2026-10-24T11:46:00'), ['ポケゲトちゃんねる'])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['event'], '抽選受付情報')
        self.assertIn('開始日時未確認', records[0]['note'])
        self.assertIn('2026/10/26 16:59', records[0]['note'])
        commit(state, records)
        self.assertEqual(plan_events(state, found, time('2026-10-25T11:36:00'), []), [])

    def test_first_discovery_official_inside_october_window(self):
        offer = application_offer(official(), FIRST_URL, PRODUCT)
        self.assertEqual(offer['start'], '2026-10-23T16:00:00+09:00')
        self.assertEqual(offer['end'], '2026-10-26T16:59:00+09:00')
        records = plan_events(ready(), [offer], time('2026-10-24T11:46:00'), [SOURCE])
        self.assertEqual(len(records), 1)
        self.assertIn('公式の応募受付期間', records[0]['note'])

    def test_expired_first_round_never_notifies_in_november(self):
        found, _ = pokeget_offers(summary())
        records = plan_events(ready(), found + [application_offer(official(), FIRST_URL, PRODUCT)], time('2026-11-06T17:00:00'), [SOURCE])
        self.assertEqual(records, [])

    def test_day_only_start_not_assumed_to_mean_midnight(self):
        found, _ = pokeget_offers(summary('11月6日(金)〜11月10日(火)16時59分', '2回目'))
        self.assertEqual(found[0]['start_precision'], 'day')
        self.assertEqual(at_time(found[0], time('2026-11-06T11:55:00')), 'unknown')
        self.assertEqual(plan_events(ready(), found, time('2026-11-06T11:55:00'), []), [])

    def test_second_round_starts_at_1600_and_sends_once(self):
        found, _ = pokeget_offers(summary('11月6日(金)〜11月10日(火)16時59分', '2回目'))
        precise = application_offer(official('11月6日(金)16時00分〜11月10日(火)16時59分'), SECOND_URL, PRODUCT)
        state = ready()
        self.assertEqual(plan_events(state, found + [precise], time('2026-11-06T11:55:00'), [SOURCE]), [])
        records = plan_events(state, found + [precise], time('2026-11-06T16:01:00'), [SOURCE])
        self.assertEqual(len(records), 1)
        self.assertIn('2回目', records[0]['title'])
        self.assertIn('2026/11/06 16:00', records[0]['note'])
        commit(state, records)
        self.assertEqual(plan_events(state, found + [precise], time('2026-11-07T09:00:00'), [SOURCE]), [])

    def test_official_expiry_overrides_a_conflicting_open_summary(self):
        old = application_offer(official(), FIRST_URL, PRODUCT)
        false_summary = dict(old, direct=False, source='ポケゲトちゃんねる', end='2026-11-10T16:59:00+09:00')
        self.assertEqual(plan_events(ready(), [false_summary, old], time('2026-11-06T17:00:00'), [SOURCE]), [])

    def test_existing_notice_not_resent_on_official_time_correction(self):
        state = ready()
        state['events']['ポケモンセンターオンライン:ハドウシーカー:lottery'] = {
            'status': 'open', 'start': '2026-11-06T00:00:00+09:00', 'end': '2026-11-10T16:59:00+09:00', 'title': NAME}
        precise = application_offer(official('11月6日(金)16時00分〜11月10日(火)16時59分'), SECOND_URL, PRODUCT)
        self.assertEqual(plan_events(state, [precise], time('2026-11-06T17:00:00'), [SOURCE]), [])
        self.assertEqual(state['events']['ポケモンセンターオンライン:ハドウシーカー:lottery']['start'], precise['start'])

    def test_full_period_after_deadline_notice_not_double_sent(self):
        state = ready()
        found, _ = pokeget_offers(summary())
        records = plan_events(state, found, time('2026-10-24T11:46:00'), [])
        commit(state, records)
        self.assertEqual(plan_events(state, [application_offer(official(), FIRST_URL, PRODUCT)], time('2026-10-25T09:00:00'), [SOURCE]), [])

    def test_deadline_extension_does_not_create_another_notice(self):
        state = ready()
        found, _ = pokeget_offers(summary())
        commit(state, plan_events(state, found, time('2026-10-24T11:46:00'), []))
        extended = dict(found[0], end='2026-10-28T16:59:00+09:00')
        self.assertEqual(plan_events(state, [extended], time('2026-10-27T09:00:00'), []), [])
        self.assertEqual(plan_events(state, [extended], time('2026-10-28T09:00:00'), []), [])

    def test_failed_delivery_stays_retryable(self):
        state = ready(); found, _ = pokeget_offers(summary())
        first = plan_events(state, found, time('2026-10-24T11:46:00'), [])
        again = plan_events(state, found, time('2026-10-24T11:47:00'), [])
        self.assertEqual(first[0]['key'], again[0]['key'])
        self.assertEqual(first[0]['end'], again[0]['end'])


class CacheMigrationTests(unittest.TestCase):
    def test_migration_prioritizes_live_pco_and_bounds_old_body_fetches(self):
        state = ready()
        index = []
        for i in range(12):
            key = str(i)
            index.append({'id': i, 'modified_gmt': '2026-01-%02dT00:00:00' % (i + 1)})
            state['posts'][key] = {'modified': index[-1]['modified_gmt'], 'offers': []}
        state['posts']['0']['offers'] = [{'retailer': 'ポケモンセンターオンライン', 'end': '2026-11-10T16:59:00+09:00'}]
        ids = parser_refresh_ids(index, state, time('2026-11-06T17:00:00'))
        self.assertEqual(len(ids), 5)
        self.assertIn('0', ids)
        for key in ids:
            state['posts'][key]['parser_version'] = 2
        self.assertFalse(ids & parser_refresh_ids(index, state, time('2026-11-06T17:00:00')))

    def test_legacy_votes_not_used_and_catalog_year_not_current_year(self):
        found, catalog = pokeget_offers(summary())
        offer = dict(found[0], legacy_unverified=True)
        self.assertEqual(plan_events(ready(), [offer], time('2026-10-24T11:46:00'), []), [])
        migrated = legacy_catalog({'offers': found})
        self.assertEqual(migrated['release_year'], 2026)
        self.assertIsNone(legacy_catalog({'offers': []})['release_year'])

    def test_wrong_release_year_weekday_cannot_create_future_lottery(self):
        # December 25 was Thursday in 2025, before a January 2026 release.
        found, _ = pokeget_offers(summary('12月25日(木)16時59分まで'))
        self.assertTrue(found[0]['calendar_error'])
        state = ready()
        self.assertEqual(plan_events(state, found, time('2026-11-06T17:00:00'), []), [])
        self.assertIn('曜日', state['lottery_unresolved'][0]['reason'])

    def test_summary_sales_period_not_used_as_application_period(self):
        post = summary()
        post['content']['rendered'] = post['content']['rendered'].replace('受付期間', '販売期間')
        found, _ = pokeget_offers(post)
        self.assertIsNone(found[0]['start'])
        self.assertIsNone(found[0]['end'])
        self.assertEqual(plan_events(ready(), found, time('2026-10-24T11:46:00'), []), [])


class OfficialSourceTests(unittest.TestCase):
    def test_public_index_discovers_known_current_box_and_ignores_other_pages(self):
        home = '<link rel="contents" href="./helpfeel_articles">'
        index = '<a href="' + SECOND_URL + '">【11月6日(金)〜11月10日(火)応募受付】ハドウシーカーBOXの抽選販売</a><a href="https://example.com/">配送の問い合わせ</a>'
        get = Mock(side_effect=[(home, {}), (index, {}), (official('11月6日(金)16時〜11月10日(火)16時59分'), {})])
        offers, issues = fetch_offers(get, [PRODUCT], time('2026-11-06T17:00:00'))
        self.assertEqual(len(offers), 1)
        self.assertEqual(issues, [])
        self.assertTrue(offers[0]['direct'])

    def test_purchase_window_is_never_application_window(self):
        page = official().replace('応募受付期間', '当選者購入期間')
        with self.assertRaises(ValueError):
            application_offer(page, FIRST_URL, PRODUCT)

    def test_year_is_not_guessed_from_current_date(self):
        with self.assertRaises(ValueError):
            application_offer(official(), FIRST_URL, dict(PRODUCT, release_year=None))
        with self.assertRaises(ValueError):
            application_offer(official(), FIRST_URL, dict(PRODUCT, release_year=2025))

    def test_external_or_credential_link_rejected(self):
        for url in ['https://www.support.pokemoncenter-online.com.evil.invalid/a', 'https://user@www.support.pokemoncenter-online.com/a', 'https://www.support.pokemoncenter-online.com:8443/a']:
            with self.assertRaises(ValueError):
                official_url(url)

    def test_missing_start_time_is_reported_not_guessed(self):
        with self.assertRaises(ValueError):
            application_offer(official('11月6日(金)〜11月10日(火)16時59分'), SECOND_URL, PRODUCT)

    def test_uncertain_candidate_visible_in_daily_health(self):
        found, _ = pokeget_offers(summary('11月6日(金)〜11月10日(火)16時59分', '2回目'))
        state = ready(); plan_events(state, found, time('2026-11-06T11:55:00'), [])
        report = {'complete': True, 'notifications': 0, 'delivered_notifications': 0, 'errors': [], 'source_offers': {}, 'product_urls': 0, 'stock_unknown': 0, 'lottery_unresolved': state['lottery_unresolved']}
        _, record, _ = plan_health(fresh_health(), report, 'success', time('2026-11-06T11:55:00'), 'owner/repo', '1', '1')
        self.assertIn('未確定', record['event'])
        self.assertIn('ハドウシーカー', record['note'])
        self.assertNotIn('今回は新規情報なし', record['note'])


if __name__ == '__main__':
    unittest.main()
