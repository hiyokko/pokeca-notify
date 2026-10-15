"""Official Toretoku Pokemon PSA-oripa sale announcements, separate from BOXes."""
import re
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlsplit

from events import JST
from htmltools import Node, Tree, clean

SOURCE = 'トレトク公式ブログ'
API = 'https://www.toretoku.jp/blog/wp-json/wp/v2/'
PRODUCT = re.compile(r'(?:ポケモン(?:カード)?|ポケカ)\s*PSA\s*(?:10\s*)?(?:オリパ|くじ)(?:\s*[1-9])?', re.I)
DATE = re.compile(r'(?:(20\d{2})\s*[年/]\s*)?(\d{1,2})\s*[月/]\s*(\d{1,2})日?')
SALE = re.compile(r'発売(?:日|予定|決定)|販売(?:日|開始|予定|決定|内容)|追加販売|再販')
CLOSED = re.compile(r'完売|売り切れ|販売終了|販売中止|発売中止')


def fetch_posts(get_json):
    categories, _ = get_json(API + 'categories?slug=notice')
    if not isinstance(categories, list) or len(categories) != 1:
        raise ValueError('トレトクのお知らせカテゴリを取得できません')
    result, page = [], 1
    while True:
        query = urlencode({'categories': categories[0]['id'], 'per_page': 100, 'page': page,
                           '_fields': 'id,link,title,content,date,modified'})
        posts, headers = get_json(API + 'posts?' + query)
        pages = int(headers.get('X-WP-TotalPages', '0'))
        if not isinstance(posts, list) or not posts or not 1 <= pages <= 20:
            raise ValueError('トレトクの記事一覧を取得できません')
        result.extend(posts)
        if page >= pages:
            return result
        page += 1


def lines(node):
    """Preserve BRs so product names are not joined to unrelated prose."""
    if isinstance(node, str):
        return node
    if node.tag == 'br':
        return '\n'
    if node.tag in {'script', 'style', 'noscript'}:
        return ''
    return ''.join(lines(child) for child in node.children)


def sale_dates(text, published):
    result = []
    for match in DATE.finditer(clean(re.sub(r'[①-⑳]', ' ', text))):
        year, month, day = match.groups()
        year = int(year) if year else published.year
        # Only December announcements can anchor an omitted January to next year.
        if not match.group(1) and published.month == 12 and int(month) == 1:
            year += 1
        try:
            result.append(datetime(year, int(month), int(day), tzinfo=JST).date().isoformat())
        except ValueError:
            continue
    return result


def parse_post(post):
    url = post['link']
    if urlsplit(url).hostname != 'www.toretoku.jp' or not urlsplit(url).path.startswith('/blog/'):
        raise ValueError('トレトク以外の記事URLです')
    published = datetime.fromisoformat(post['date']).replace(tzinfo=JST)
    root = Tree(post['content']['rendered']).root
    title = Tree(post['title']['rendered']).root.text()
    # Content API excludes sidebars. Ignore TOC containers and nested table cells.
    blocks = []
    for node in root.walk():
        if node.tag not in {'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'table'}:
            continue
        parent, skip = node.parent, False
        while parent and parent is not root:
            if parent.tag in {'table', 'p'} or 'toc' in parent.attrs.get('id', '').lower():
                skip = True
            parent = parent.parent
        if not skip:
            blocks.append(node)
    found, closed_dates = [], set()

    def add(name, dates, details, explicit=False):
        if CLOSED.search(details):
            closed_dates.update(dates)
            return
        if not dates and not explicit:
            return
        for day in dates or [None]:
            fields = []
            for label in ('販売価格', '販売個数', '個数制限'):
                m = re.search(label + r'\s*[:：]\s*([^\n]+)', details)
                if m:
                    fields.append(label + ': ' + clean(m.group(1))[:90])
            found.append({'title': clean(name), 'sale_date': day,
                          'published': published.isoformat(), 'post_id': str(post['id']),
                          'url': url, 'source': SOURCE, 'retailer': 'トレトク',
                          'details': '\n'.join(fields)})

    context_dates, invalid_context = [], False
    for i, node in enumerate(blocks):
        text = node.text()
        if SALE.search(text):
            invalid_context = bool(DATE.search(text)) and not sale_dates(text, published)
        if node.tag.startswith('h'):
            context_dates = sale_dates(text, published) if SALE.search(text) else context_dates
            # A new high-level section without a sale date ends the preceding date.
            if node.tag in {'h2', 'h3'} and not SALE.search(text):
                context_dates = []
        if node.tag == 'table':
            rows = []
            for row in node.walk('tr'):
                cells = [c for c in row.children if isinstance(c, Node) and c.tag in {'td', 'th'}]
                if len(cells) >= 2:
                    rows.append((cells[0].text(), ' '.join(lines(c) for c in cells[1:])))
            if rows and '発売日' in rows[0][1]:
                for name, period in rows[1:]:
                    if PRODUCT.fullmatch(clean(name)):
                        add(name, sale_dates(period, published), '', False)
            continue
        raw_lines = [clean(line) for line in lines(node).splitlines() if clean(line)]
        if not node.tag.startswith('h') and len(text) < 70 and SALE.search(text):
            context_dates = sale_dates(text, published) or context_dates
        for line in raw_lines:
            name = line.strip('・●★☆▼ ').strip()
            if not PRODUCT.fullmatch(name):
                continue
            # Product line plus its own detail block only, never article-wide PSA matches.
            detail = list(raw_lines)
            for following in blocks[i + 1:]:
                value = following.text()
                if following.tag.startswith('h') or following.tag == 'table':
                    break
                if PRODUCT.search(value) or (SALE.search(value) and sale_dates(value, published) and not re.match(r'(?:発売日|販売日)\s*[:：]', value)):
                    break
                if re.match(r'[・●].*(?:オリパ|福袋|くじ)', value):
                    break
                detail.extend(clean(v) for v in lines(following).splitlines() if clean(v))
            detail_text = '\n'.join(detail)
            own_dates = sale_dates(detail_text, published) if SALE.search(detail_text) else []
            dates = own_dates or context_dates
            if not dates and DATE.search(text):
                continue
            explicit = (bool(SALE.search(title + '\n' + detail_text)) and '販売価格' in detail_text
                        and not DATE.search(detail_text))
            if not invalid_context:
                add(name, dates, detail_text, explicit)
    # Summary tables and detailed sections describe the same dated sale.
    unique = {}
    for item in found:
        if item['sale_date'] in closed_dates:
            continue
        key = item['sale_date'] or 'undated'
        if key not in unique or len(item['details']) > len(unique[key]['details']):
            unique[key] = item
    return list(unique.values())


def plan_announcements(state, posts, now):
    """Dated future sales alert immediately; historical entries establish a baseline."""
    tracked = state.setdefault('events', {})
    started = state.setdefault('toretoku_started_at', now.isoformat())
    started_at = datetime.fromisoformat(started)
    offers = [offer for post in posts for offer in parse_post(post)]
    grouped = {}
    for offer in offers:
        # Collapse duplicate campaign/reminder articles for a sale on the same day.
        key = 'toretoku:psa:' + (offer['sale_date'] or 'post-' + offer['post_id'])
        if key not in grouped or len(offer['details']) > len(grouped[key]['details']):
            grouped[key] = offer
    pending = []
    for key, offer in grouped.items():
        if key in tracked:
            continue
        published = datetime.fromisoformat(offer['published'])
        day = offer['sale_date']
        eligible = day >= now.date().isoformat() if day else (
            published >= started_at and published >= now - timedelta(days=7))
        # If an undated announcement was sent, specifying its date isn't a new sale.
        prior_undated = tracked.get('toretoku:psa:post-' + offer['post_id'], {})
        after = dict(offer, status='announced' if eligible else 'historical')
        if eligible and prior_undated.get('status') != 'announced':
            date_note = '販売予定日: ' + day if day else '販売日: 公式告知で未確認'
            note = date_note + '\n' + offer['details'] + '\n公式の販売告知です。在庫の有無は未確認です。\n[公式告知](' + offer['url'] + ')'
            pending.append(dict(offer, key=key, event='PSAオリパ販売告知', note=note, after=after))
        else:
            tracked[key] = after
    return pending, len(offers)
