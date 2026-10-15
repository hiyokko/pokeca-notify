import unittest
from datetime import datetime
from unittest.mock import Mock

from events import JST
from toretoku import fetch_posts, parse_post, plan_announcements


def post(body, id=1, date='2026-10-16T08:00:00', title='オリパ追加販売のお知らせ'):
    return {'id': id, 'date': date, 'modified': date, 'link': 'https://www.toretoku.jp/blog/notice/post-%s/' % id,
            'title': {'rendered': title}, 'content': {'rendered': body}}


def sale(day='10/20', price='14000', title='ポケモンカードPSA10オリパ'):
    return '<h3>%s発売予定</h3><h4>・%s</h4><p>販売価格：%s円</p><p>販売個数：50個</p>' % (day, title, price)


class ParsingTests(unittest.TestCase):
    def test_campaign_rounds_and_table_duplicates(self):
        body = '<table><tr><td>商品名</td><td>発売日</td></tr><tr><td>ポケモンカードPSA10オリパ</td><td>①10/20 ②10/27</td></tr></table>'
        body += sale(title='ポケモンPSAオリパ①') + sale('10/27', title='ポケモンカードPSA10オリパ ②')
        items = parse_post(post(body))
        self.assertEqual([x['sale_date'] for x in items], ['2026-10-20', '2026-10-27'])
        self.assertTrue(all('14000' in x['details'] for x in items))

    def test_br_based_psa_kuji(self):
        p = post('<p>▼オリパ販売内容<br>ポケモンカード PSA10くじ<br>発売日：10/23（水）<br>販売価格：13,500円<br>販売個数：100個</p>')
        item = parse_post(p)[0]
        self.assertEqual(item['sale_date'], '2026-10-23')
        self.assertIn('13,500円', item['details'])

    def test_paragraph_based_extra_sale(self):
        p = post('<p>☆★10/28(月)発売予定★☆</p><p>・ポケモンカードPSA10オリパ</p><p>販売価格：14,000円</p>')
        self.assertEqual(parse_post(p)[0]['sale_date'], '2026-10-28')

    def test_date_in_next_paragraph(self):
        p = post('<h4>ポケモンPSAオリパ</h4><p>発売日：10/30</p><p>販売価格：10000円</p>')
        self.assertEqual(parse_post(p)[0]['sale_date'], '2026-10-30')

    def test_excludes_normal_oripa_other_games_and_psa_boilerplate(self):
        body = sale(title='遊戯王PSA10オリパ') + sale(title='ポケモンカード1,000円オリパ')
        body += '<p>※こちらのくじの鑑定品（PSA等）は最高評価ではないものも含まれています。</p>'
        body += '<p>以前実施したポケモンPSA10くじをご紹介します。</p>'
        self.assertEqual(parse_post(post(body)), [])

    def test_article_update_does_not_change_year(self):
        p = post(sale('10/23'), date='2024-10-16T14:00:00')
        p['modified'] = '2026-10-16T14:00:00'
        self.assertEqual(parse_post(p)[0]['sale_date'], '2024-10-23')

    def test_new_year_and_invalid_date(self):
        self.assertEqual(parse_post(post(sale('1/3'), date='2026-12-20T14:00:00'))[0]['sale_date'], '2027-01-03')
        self.assertEqual(parse_post(post(sale('2027年2月30日'))), [])

    def test_circled_rounds_in_summary_table(self):
        body = '<table><tr><td>商品名</td><td>発売日</td></tr><tr><td>ポケモンカードPSA10オリパ</td><td>①7/6 ②7/13 ③7/21</td></tr></table>'
        self.assertEqual([x['sale_date'] for x in parse_post(post(body))], ['2026-07-06', '2026-07-13', '2026-07-21'])

    def test_closed_sale(self):
        table = '<table><tr><td>商品名</td><td>発売日</td></tr><tr><td>ポケモンPSAオリパ</td><td>10/20</td></tr></table>'
        self.assertEqual(parse_post(post(table + sale() + '<p>完売しました</p>')), [])

    def test_official_domain_only(self):
        p = post(sale()); p['link'] = 'https://www.toretoku.jp.evil.invalid/blog/post-1/'
        with self.assertRaises(ValueError):
            parse_post(p)


class PlanningTests(unittest.TestCase):
    now = datetime(2026, 10, 16, 9, tzinfo=JST)

    def test_initial_historical_baseline_future_alert(self):
        state = {}
        pending, count = plan_announcements(state, [post(sale('7/6'), id=1), post(sale(), id=2)], self.now)
        self.assertEqual(count, 2)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['sale_date'], '2026-10-20')

    def test_delivery_retry_and_price_update_are_idempotent(self):
        state = {}
        first, _ = plan_announcements(state, [post(sale())], self.now)
        retry, _ = plan_announcements(state, [post(sale())], self.now)
        self.assertEqual(first, retry)
        state['events'][first[0]['key']] = first[0]['after']
        self.assertEqual(plan_announcements(state, [post(sale(price='15000'))], self.now)[0], [])
        self.assertEqual(len(plan_announcements(state, [post(sale('10/27'))], self.now)[0]), 1)

    def test_same_day_multiple_articles_one_notice(self):
        pending, _ = plan_announcements({}, [post(sale()), post(sale(), id=2)], self.now)
        self.assertEqual(len(pending), 1)

    def test_undated_new_announcement_then_date_added(self):
        state = {'toretoku_started_at': '2026-10-15T09:00:00+09:00'}
        p = post('<h4>ポケモンPSAオリパ</h4><p>販売価格：14000円</p>')
        pending, _ = plan_announcements(state, [p], self.now)
        self.assertEqual(len(pending), 1)
        state['events'][pending[0]['key']] = pending[0]['after']
        self.assertEqual(plan_announcements(state, [post(sale())], self.now)[0], [])

    def test_old_undated_never_becomes_new_on_article_edit(self):
        p = post('<h4>ポケモンPSAオリパ</h4><p>販売価格：14000円</p>', date='2024-10-16T09:00:00')
        self.assertEqual(plan_announcements({}, [p], self.now)[0], [])

    def test_all_category_pages_and_failure(self):
        api = Mock(side_effect=[([{'id': 15}], {}), ([post(sale())], {'X-WP-TotalPages': '2'}), ([post(sale(), id=2)], {'X-WP-TotalPages': '2'})])
        self.assertEqual(len(fetch_posts(api)), 2)
        self.assertIn('page=2', api.call_args.args[0])
        with self.assertRaises(ValueError):
            fetch_posts(Mock(return_value=([], {})))


if __name__ == '__main__':
    unittest.main()
