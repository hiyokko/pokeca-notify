import base64
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from adapters import article_records, canonical_product, official_records, product_kind, stock_status
from monitor import StateStore, changed_record, fresh_state, send_discord, stock_transition, validate_state


def article(spec='1BOX(30パック)', extra='', title='『テスト拡張パック』予約/抽選情報まとめ'):
    return {'id': 12, 'link': 'https://pokemon-infomation.com/sample/', 'title': {'rendered': title},
            'content': {'rendered': '<table><tr><th>商品名</th><td>テスト</td></tr><tr><th>価格</th><td>' + spec +
            '</td></tr></table><h3>Amazonの予約/抽選情報</h3><table><tr><th>販売形式</th><td>抽選販売</td></tr>' +
            '<tr><th>受付期間</th><td>10月20日まで</td></tr></table>' + extra +
            '<h3>楽天ブックスの予約/抽選情報</h3><table><tr><th>受付期間</th><td>11月1日まで</td></tr></table>'}}


class ParsingTests(unittest.TestCase):
    def test_box_and_box_sets(self):
        self.assertEqual(product_kind('拡張パック', '１ＢＯＸ'), 'box')
        self.assertEqual(product_kind('セット', '拡張パック2BOX'), 'box_set')
        self.assertEqual(product_kind('ルリナセット', ''), 'box_set')
        self.assertEqual(product_kind('イーブイズセット', ''), 'box_set')

    def test_loose_pack_sets_and_accessories(self):
        self.assertEqual(product_kind('ファイルセット', '拡張パック6パック'), 'review')
        self.assertEqual(product_kind('BOX収納ケース', '1BOX'), 'excluded')
        self.assertEqual(product_kind('空箱 BOX', ''), 'excluded')
        self.assertEqual(product_kind('スタートデッキ100', '1BOX'), 'excluded')

    def test_only_selected_retailer_section(self):
        records, _ = article_records(article())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['retailer'], 'Amazon')
        changed = article()
        changed['content']['rendered'] = changed['content']['rendered'].replace('11月1日', '12月1日')
        self.assertEqual(records[0]['signature'], article_records(changed)[0][0]['signature'])

    def test_advertisements_are_not_box_evidence(self):
        records, catalog = article_records(article('6パック', '<aside>おすすめの1BOX</aside>', 'ファイルセット 予約情報'))
        self.assertEqual(records, [])
        self.assertEqual(catalog['kind'], 'review')

    def test_pack_only_offer_is_not_notified(self):
        post = article()
        post['content']['rendered'] = post['content']['rendered'].replace('<th>販売形式</th><td>抽選販売', '<th>販売数量</th><td>1人5パック')
        self.assertEqual(article_records(post)[0], [])

    def test_canonical_amazon_urls_remove_affiliate_params(self):
        self.assertEqual(canonical_product('https://www.amazon.co.jp/gp/product/B012345678?tag=test'), 'https://www.amazon.co.jp/dp/B012345678')
        self.assertIsNone(canonical_product('https://amazon.co.jp.evil.invalid/dp/B012345678'))
        self.assertIsNone(canonical_product('https://www.amazon.co.jp/s?k=BOX'))

    def test_nested_rounds_stay_in_same_retailer_section(self):
        post = article(extra='<h4>第2回</h4><table><tr><th>受付期間</th><td>12月まで</td></tr></table>')
        records, _ = article_records(post)
        self.assertNotEqual(records[0]['signature'], article_records(article())[0][0]['signature'])

    def test_official_failure_is_not_empty_success(self):
        with self.assertRaises(ValueError):
            official_records('<html>maintenance</html>')

    def test_generic_official_notice_is_not_misclassified_as_box(self):
        self.assertEqual(official_records('<div id="newsTab_all"><a class="List_item_inner" href="/info/a"><img alt="商品の抽選について"></a></div>'), [])

    def test_stock_requires_amazon_seller_and_enabled_button(self):
        html = '<span id="productTitle">テストBOX</span><div id="availability">在庫あり</div><a id="sellerProfileTriggerId">Amazon.co.jp</a><input id="add-to-cart-button">'
        self.assertEqual(stock_status(html, 'https://www.amazon.co.jp/dp/B012345678'), 'available')
        self.assertEqual(stock_status(html.replace('Amazon.co.jp</a>', '別の販売者</a>'), 'https://www.amazon.co.jp/dp/B012345678'), 'unavailable')
        self.assertEqual(stock_status(html.replace('id="add-to-cart-button"', 'id="add-to-cart-button" disabled'), 'https://www.amazon.co.jp/dp/B012345678'), 'unknown')
        self.assertEqual(stock_status('<form id="captchacharacters">', 'https://www.amazon.co.jp/dp/B012345678'), 'unknown')


class StateTests(unittest.TestCase):
    def test_initial_baseline_and_repeat(self):
        state = fresh_state()
        record = {'key': 'a', 'signature': 'x'}
        self.assertFalse(changed_record(state, record, baseline=True))
        state['records']['a'] = 'x'
        self.assertFalse(changed_record(state, record))
        record['signature'] = 'y'
        self.assertTrue(changed_record(state, record))
        self.assertEqual(state['records']['a'], 'x')  # not sent yet

    def test_unknown_preserves_last_confirmed_stock(self):
        state = fresh_state()
        self.assertFalse(stock_transition(state, 'u', 'unavailable'))
        self.assertFalse(stock_transition(state, 'u', 'unknown'))
        self.assertEqual(state['stock']['u'], 'unavailable')
        self.assertTrue(stock_transition(state, 'u', 'available'))
        self.assertFalse(stock_transition(state, 'u', 'available'))

    def test_first_available_is_not_a_restock(self):
        self.assertFalse(stock_transition(fresh_state(), 'u', 'available'))

    def test_corrupt_state_fails_closed(self):
        with self.assertRaises(ValueError):
            validate_state({'version': 1})

    def test_local_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / 'state.json')
            state = store.load()
            state['records']['a'] = 'x'
            store.save(state)
            self.assertEqual(store.load(), state)

    @patch.dict(os.environ, {'GITHUB_REPOSITORY': 'user/repo', 'GITHUB_TOKEN': 'fake'})
    def test_missing_state_on_existing_branch_is_not_reset(self):
        store = StateStore(github=True)
        error = HTTPError('https://api.github.com/', 404, 'Not found', {}, None)
        with patch.object(store, 'api', side_effect=[error, {'ref': 'heads/monitor-state'}]):
            with self.assertRaises(ValueError):
                store.load()

    @patch.dict(os.environ, {'GITHUB_REPOSITORY': 'user/repo', 'GITHUB_TOKEN': 'fake'})
    def test_github_state_decode(self):
        state = fresh_state()
        store = StateStore(github=True)
        data = {'sha': 'abc', 'content': base64.b64encode(json.dumps(state).encode()).decode()}
        with patch.object(store, 'api', return_value=data):
            self.assertEqual(store.load(), state)
            self.assertEqual(store.sha, 'abc')


class DiscordTests(unittest.TestCase):
    webhook = 'https://discord.com/api/webhooks/123/fake_token'
    record = {'title': '@everyone テスト', 'url': 'https://example.com/'}

    def test_wait_and_disabled_mentions(self):
        with patch('monitor.request', return_value=('{"id":"123"}', {})) as request:
            send_discord([self.record], self.webhook)
        url, payload = request.call_args.args
        self.assertTrue(url.endswith('?wait=true'))
        self.assertEqual(payload['allowed_mentions'], {'parse': []})

    def test_rate_limit_retry(self):
        error = HTTPError(self.webhook, 429, 'Rate limited', {}, io.BytesIO(b'{"retry_after":0}'))
        with patch('monitor.request', side_effect=[error, ('{"id":"1"}', {})]) as request, patch('monitor.time.sleep'):
            send_discord([self.record], self.webhook)
            self.assertEqual(request.call_count, 2)

    def test_no_retry_for_deleted_webhook(self):
        error = HTTPError(self.webhook, 404, 'Gone', {}, None)
        with patch('monitor.request', side_effect=error) as request:
            with self.assertRaises(HTTPError):
                send_discord([self.record], self.webhook)
            self.assertEqual(request.call_count, 1)

    def test_reject_credential_redirect_destination(self):
        with self.assertRaises(ValueError):
            send_discord([self.record], 'https://discord.com.evil.invalid/api/webhooks/123/token')


if __name__ == '__main__':
    unittest.main()
