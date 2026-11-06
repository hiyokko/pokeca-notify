"""Discover BOX lottery application periods from the public official support index."""
import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urldefrag

from events import JST, date_range, identity, observation
from htmltools import Tree, table_rows

ROOT = 'https://www.support.pokemoncenter-online.com/'
HOST = 'www.support.pokemoncenter-online.com'
SOURCE = 'ポケモンセンター公式FAQ'


def official_url(href):
    url = urldefrag(urljoin(ROOT, href))[0]
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc != HOST or parsed.username or parsed.password:
        raise ValueError('公式FAQ以外へのリンクです')
    return url


def match_product(title, catalog):
    text = identity(title)
    choices = [p for p in catalog if p.get('kind') in {'box', 'box_set'} and
               len(identity(p['title'])) >= 3 and identity(p['title']) in text]
    return max(choices, key=lambda p: len(identity(p['title']))) if choices else None


def application_offer(html, url, product):
    root = Tree(html).root
    title = next((n.text() for n in root.walk('h1') if 'BOX' in n.text().upper() and '抽選' in n.text()), '')
    if not title or not match_product(title, [product]):
        raise ValueError('公式FAQの商品を照合できません')
    rows = [pair for table in root.walk('table') for pair in table_rows(table)]
    periods = [v for k, v in rows if k in {'応募受付期間', '抽選応募期間', '申込受付期間'}]
    if len(periods) != 1:
        raise ValueError('公式FAQの応募受付期間が一意ではありません')
    period = periods[0]
    start, end = date_range(period, product.get('release_year'))
    first = re.split('[~〜～]', period, maxsplit=1)[0]
    if not start or not end or not re.search(r'\d{1,2}(?:時|:)', first):
        raise ValueError('公式FAQの開始日時または終了日時を確定できません')
    if start >= end:
        raise ValueError('公式FAQの応募期間が逆転しています')
    dates = [datetime.fromisoformat(start), datetime.fromisoformat(end)]
    weekdays = re.findall(r'\d{1,2}月\d{1,2}日\(([月火水木金土日])\)', period)
    if weekdays and (len(weekdays) != 2 or any('月火水木金土日'[dt.weekday()] != day for dt, day in zip(dates, weekdays))):
        raise ValueError('公式FAQの日付と曜日が一致しません。年の照合が必要です')
    item = observation(product['title'], 'ポケモンセンターオンライン', 'lottery', SOURCE,
                       official_url(url), official_url(url), start=start, end=end, direct=True, period=period)
    item['start_precision'] = 'minute'
    item['application_period_verified'] = True
    return item


def fetch_offers(get, catalog, now):
    home, _ = get(ROOT)
    root = Tree(home).root
    contents = [n.attrs.get('href', '') for n in root.walk('link') if 'contents' in n.attrs.get('rel', '').split()]
    if len(contents) != 1:
        raise ValueError('公式FAQの記事一覧リンクを取得できません')
    index, _ = get(official_url(contents[0]))
    entries = {}
    for a in Tree(index).root.walk('a'):
        title = a.text()
        if '応募受付' not in title or '抽選' not in title or 'BOX' not in title.upper():
            continue
        product = match_product(title, catalog)
        if not product:
            continue
        url = official_url(a.attrs.get('href', ''))
        if not re.search(r'-[0-9a-f]{24}$', urlsplit(url).path):
            raise ValueError('公式FAQの記事URL形式が変わりました')
        entries[url] = (title, product)
    if not entries:
        raise ValueError('既知のBOXに対応する公式抽選記事が見つかりません')
    if len(entries) > 40:
        raise ValueError('公式FAQの対象記事数が上限を超えました')
    offers, issues = [], []
    for url, (title, product) in entries.items():
        # Year is anchored to the catalog, never to the current wall-clock year.
        start, end = date_range(title, product.get('release_year'))
        if end and datetime.fromisoformat(end) < now:
            continue
        try:
            html, _ = get(url)
            offers.append(application_offer(html, url, product))
        except Exception as error:
            # Preserve the working articles and surface partial source failures.
            issues.append({'title': product['title'], 'retailer': 'ポケモンセンターオンライン',
                           'reason': '公式応募期間を確認できません: ' + type(error).__name__, 'source_url': url})
    return offers, issues
