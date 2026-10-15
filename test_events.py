import copy
import unittest
from datetime import datetime
from events import (JST, at_time, observation, identity, plan_events, date_range,
                    pokeget_offers, mirar_offers, board_offers, cardchusen_offers)
from monitor import fresh_state

NOW = datetime(2026, 10, 16, 9, tzinfo=JST)

def offer(**kwargs):
    data = observation('拡張パック「テスト」BOX', 'ポケモンセンターオンライン', 'lottery',
                       'ポケゲトちゃんねる', 'https://example.com/',
                       start='2026-10-16T08:00:00+09:00', end='2026-10-20T16:59:00+09:00')
    data.update(kwargs)
    return data

def ready():
    state = fresh_state()
    state['started_at'] = '2026-10-01T00:00:00'
    state['initialized_sources'] = ['ポケゲトちゃんねる', 'Mirar', 'ポケカ抽選ボード', 'Amazon公式']
    return state

class PurchaseEventTests(unittest.TestCase):
    def test_first_run_baselines_ongoing_lottery(self):
        state = fresh_state()
        self.assertEqual(plan_events(state, [offer()], NOW, ['ポケゲトちゃんねる']), [])
        self.assertEqual(len(state['events']), 1)

    def test_new_dated_round_sends_once_and_retries_until_delivery(self):
        state = ready()
        pending = plan_events(state, [offer()], NOW, ['ポケゲトちゃんねる'])
        self.assertEqual(pending[0]['event'], '抽選開始')
        self.assertEqual(len(plan_events(state, [offer()], NOW, [])), 1)
        state['events'][pending[0]['key']] = pending[0]['after']
        changed = offer(period='価格や条件を編集', end='2026-10-22T16:59:00+09:00')
        self.assertEqual(plan_events(state, [changed], NOW, []), [])

    def test_scheduled_start_fires_without_article_update(self):
        state = ready()
        self.assertEqual(plan_events(state, [offer()], NOW.replace(day=15), []), [])
        self.assertEqual(len(plan_events(state, [offer()], NOW, [])), 1)

    def test_expired_and_unknown_do_not_alert(self):
        for changed in [offer(end='2026-10-15T16:59:00+09:00'), offer(start=None, end=None)]:
            self.assertEqual(plan_events(ready(), [changed], NOW, []), [])

    def test_single_undated_summary_is_not_proof_of_start(self):
        self.assertEqual(plan_events(ready(), [offer(start=None, end=None, status='open')], NOW, []), [])

    def test_multiple_sources_deduplicated_by_product_retailer(self):
        state = ready()
        first = offer(retailer='Amazon', start=None, end=None, source='Mirar', status='open')
        second = dict(first, source='ポケカ抽選ボード', title='テスト BOX',
                      url='https://www.amazon.co.jp/dp/B012345678')
        pending = plan_events(state, [first, second], NOW, [])
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['url'], second['url'])

    def test_new_source_does_not_create_fake_start(self):
        state = ready()
        state['initialized_sources'] = ['Mirar']
        a = offer(source='Mirar', status='open', start=None, end=None)
        b = dict(a, source='new source')
        self.assertEqual(plan_events(state, [a, b], NOW, ['new source']), [])

    def test_price_title_deadline_or_start_correction_not_new_round(self):
        state = ready()
        base = offer()
        pending = plan_events(state, [base], NOW, [])
        state['events'][pending[0]['key']] = pending[0]['after']
        self.assertEqual(plan_events(state, [dict(base, start='2026-10-16T07:00:00+09:00')], NOW, []), [])

    def test_expired_round_extension_is_not_new_lottery(self):
        state = ready()
        base = offer(start='2026-10-01T08:00:00+09:00', end='2026-10-15T16:59:00+09:00')
        plan_events(state, [base], NOW, [])
        self.assertEqual(plan_events(state, [dict(base, end='2026-10-20T16:59:00+09:00')], NOW, []), [])

    def test_next_round_after_previous_end(self):
        state = ready()
        past = offer(start='2026-10-01T08:00:00+09:00', end='2026-10-05T16:59:00+09:00')
        plan_events(state, [past], NOW, [])
        self.assertEqual(len(plan_events(state, [past, offer()], NOW, [])), 1)

    def test_unknown_stock_preserves_then_restock(self):
        state = ready()
        closed = offer(kind='sale', source='Amazon公式', retailer='Amazon', start=None, end=None, status='closed', direct=True)
        plan_events(state, [closed], NOW, [])
        self.assertEqual(plan_events(state, [dict(closed, status='unknown')], NOW, []), [])
        pending = plan_events(state, [dict(closed, status='open')], NOW, [])
        self.assertEqual(pending[0]['event'], '再販・在庫復活')

    def test_initial_available_is_not_restock(self):
        self.assertEqual(plan_events(ready(), [offer(kind='sale', direct=True, source='Amazon公式',
                          start=None, end=None, status='open')], NOW, []), [])

    def test_amazon_link_never_used_for_pokemon_center(self):
        pc = offer(url='https://www.pokemoncenter-online.com/lottery/apply.html')
        amazon = offer(retailer='Amazon', url='https://www.amazon.co.jp/dp/B012345678')
        pending = plan_events(ready(), [amazon, pc], NOW, [])
        result = next(p for p in pending if p['retailer'] != 'Amazon')
        self.assertEqual(result['url'], pc['url'])

class EventParserTests(unittest.TestCase):
    def test_dates_and_year_rollover(self):
        a, b = date_range('2026年12月30日(水)12時〜1月2日(土)16時59分', 2026)
        self.assertEqual(a, '2026-12-30T12:00:00+09:00')
        self.assertEqual(b, '2027-01-02T16:59:00+09:00')
        self.assertEqual(date_range('10月1日〜10月5日'), (None, None))
        self.assertEqual(date_range('2026年2月30日〜3月5日'), (None, None))

    def test_normalized_product_identity(self):
        self.assertEqual(identity('拡張パック『テスト』'), identity('テスト BOX'))

    def test_table_isolated_from_other_retailers_and_purchase_window(self):
        html = '<table><tr><td>商品名</td><td>テスト</td></tr><tr><td>発売日</td><td>2026年9月1日</td></tr><tr><td>価格</td><td>1BOX</td></tr></table>'
        table = '<table><tr><td>販売形式</td><td>抽選販売</td></tr><tr><td>受付期間</td><td>10月1日〜10月5日</td></tr><tr><td>購入期間</td><td>10月15日〜10月20日</td></tr></table>'
        html += '<h3>ポケモンセンターオンライン</h3><h4>第1回</h4>' + table + '<h3>楽天ブックス</h3>' + table
        post = dict(title={'rendered':'抽選'}, content={'rendered':html}, link='https://example.com/')
        offers, _ = pokeget_offers(post)
        self.assertEqual(len(offers), 1)
        self.assertEqual(at_time(offers[0], NOW), 'closed')

    def test_unrelated_updates_never_change_purchase_event(self):
        html = '<table><tr><td>商品名</td><td>テスト</td></tr><tr><td>価格</td><td>1BOX</td></tr></table><h3>Amazon</h3><table><tr><td>販売形式</td><td>抽選</td></tr><tr><td>受付期間</td><td>2026年10月1日〜10月20日</td></tr><tr><td>販売価格</td><td>5000円</td></tr></table>'
        p = dict(title={'rendered':'抽選'}, content={'rendered':html}, link='https://example.com/')
        a, _ = pokeget_offers(p)
        p['content']['rendered'] = html.replace('5000円','6000円') + '<aside>他店の再販</aside>'
        b, _ = pokeget_offers(p)
        self.assertEqual(a, b)

    def test_mirar_ignores_pack_sets_and_unrelated_tables(self):
        html = '<div class="cps-post-main"><table><tr><th>商品名</th><th>Amazonの状況</th></tr><tr><td>テスト BOX</td><td>受付中</td></tr><tr><td>ファイルセット</td><td>受付中</td></tr><tr><td>別のBOX</td><td>受付終了</td></tr></table></div>'
        offers = mirar_offers(html)
        self.assertEqual([o['status'] for o in offers], ['open','closed'])
        with self.assertRaises(ValueError):mirar_offers('<html>maintenance</html>')

    def test_board_mixed_games_and_sidebar_ads_excluded(self):
        card = '<a class="azi" href="https://www.amazon.co.jp/dp/B012345678"><b>{}</b></a>'
        html = '<section id="amazon-invite">' + card.format('テスト BOX') + card.format('ワンピース BOX') + '</section>' + card.format('広告 BOX')
        self.assertEqual(len(board_offers(html, {identity('テスト')})), 1)

if __name__ == '__main__':
    unittest.main()
