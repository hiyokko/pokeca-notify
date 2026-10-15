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

from adapters import canonical_product, product_kind, stock_status
from monitor import StateStore, fresh_state, send_discord, validate_state


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




    def test_canonical_amazon_urls_remove_affiliate_params(self):
        self.assertEqual(canonical_product('https://www.amazon.co.jp/gp/product/B012345678?tag=test'), 'https://www.amazon.co.jp/dp/B012345678')
        self.assertIsNone(canonical_product('https://amazon.co.jp.evil.invalid/dp/B012345678'))
        self.assertIsNone(canonical_product('https://www.amazon.co.jp/s?k=BOX'))




    def test_stock_requires_amazon_seller_and_enabled_button(self):
        html = '<span id="productTitle">テストBOX</span><div id="availability">在庫あり</div><a id="sellerProfileTriggerId">Amazon.co.jp</a><input id="add-to-cart-button">'
        self.assertEqual(stock_status(html, 'https://www.amazon.co.jp/dp/B012345678'), 'available')
        self.assertEqual(stock_status(html.replace('Amazon.co.jp</a>', '別の販売者</a>'), 'https://www.amazon.co.jp/dp/B012345678'), 'unavailable')
        self.assertEqual(stock_status(html.replace('id="add-to-cart-button"', 'id="add-to-cart-button" disabled'), 'https://www.amazon.co.jp/dp/B012345678'), 'unknown')
        self.assertEqual(stock_status('<form id="captchacharacters">', 'https://www.amazon.co.jp/dp/B012345678'), 'unknown')


class StateTests(unittest.TestCase):



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
