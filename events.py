"""Purchase events, not document-change notifications. All dates are Japan time."""
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from htmltools import Tree, clean, table_rows
from adapters import canonical_product, product_kind, fingerprint, SITE

JST = timezone(timedelta(hours=9))
PARSER_VERSION = 2
SOURCES = {
    'board': ('ポケカ抽選ボード', 'https://chusen-board.com/'),
    'mirar': ('Mirar', 'https://otoku-mirar.com/amazon-pokemon-card-request-sell'),
    'cardchusen': ('カード抽選まとめ', 'https://cardchusen.com/'),
}


def identity(title):
    text = clean(title).lower()
    for prefix in ('ポケモンカードゲーム', 'ポケモンカード', 'スカーレット&バイオレット',
                   'ソード&シールド', 'サン&ムーン', 'ハイクラスパック', '強化拡張パック',
                   '拡張パック', 'mega '):
        text = text.replace(prefix.lower(), '')
    text = re.sub(r'(?:\d+\s*)?box|ボックス', '', text)
    return re.sub(r'[\s「」『』【】\[\]（）()・:：]+', '', text)


def observation(title, retailer, kind, source, source_url, url='', status='unknown',
                start=None, end=None, direct=False, period=''):
    return dict(title=clean(title), product=identity(title), retailer=retailer, kind=kind,
                source=source, source_url=source_url, url=url or source_url,
                status=status, start=start, end=end, direct=direct, period=period)


def date_range(text, year=None):
    """No guessed dates for undated listings; a year must be supplied by product specs."""
    text = clean(text)
    pattern = r'(?:(20\d{2})[年/.-])?(\d{1,2})[月/.-](\d{1,2})日?(?:\([^)]*\))?\s*(?:(\d{1,2})[時:](?:(\d{1,2})分?)?)?'
    matches = list(re.finditer(pattern, text))
    if not matches or len(matches) > 2:
        return None, None
    values = []
    for i, match in enumerate(matches):
        y, month, day, hour, minute = match.groups()
        y = int(y) if y else year
        if not y:
            return None, None
        # A year crossing is valid only in a two-ended range.
        if values and not match[1] and int(month) < values[0].month:
            y = values[0].year + 1
        try:
            values.append(datetime(y, int(month), int(day), int(hour or (23 if i else 0)),
                                   int(minute or (59 if i else 0)), tzinfo=JST))
        except ValueError:
            return None, None
    if len(values) == 1 and re.search(r'まで|〆|締切', text) and not re.search(r'から|[~〜～]', text):
        return None, (values[0] if matches[0][4] else values[0].replace(hour=23, minute=59)).isoformat()
    return values[0].isoformat(), values[1].isoformat() if len(values) == 2 else None


def at_time(offer, now):
    if offer.get('tentative') or offer.get('calendar_error') or offer.get('legacy_unverified'):
        return 'unknown'
    if offer['status'] == 'closed':
        return 'closed'
    start = datetime.fromisoformat(offer['start']) if offer.get('start') else None
    end = datetime.fromisoformat(offer['end']) if offer.get('end') else None
    if end and now > end:
        return 'closed'
    if start and now < start:
        return 'scheduled'
    if start and offer.get('start_precision') == 'day' and now.date() == start.date() and offer['status'] != 'open':
        return 'unknown'
    if not start and end and offer.get('application_deadline') and offer['status'] != 'open':
        return 'deadline_known'
    # A dated lottery with no deadline could have ended silently. Require an
    # explicit current status; a historical '6/2〜' alone is not proof of open.
    if start and end:
        return 'open'
    return offer['status']


def pokeget_offers(post):
    root = Tree(post['content']['rendered']).root
    article_title = Tree(post['title']['rendered']).root.text()
    specs = [row for t in root.walk('table') if any(k == '商品名' for k, _ in table_rows(t))
             for row in table_rows(t)]
    title = next((v for k, v in specs if k == '商品名'), article_title)
    details = ' '.join(v for _, v in specs)
    kind = product_kind(title, details)
    catalog = dict(title=title, url=post['link'], kind=kind)
    if kind not in {'box', 'box_set'}:
        return [], catalog
    # Release-date year anchors yearless tables. Do not use article modified
    # year, which could turn an old round into a current round.
    years = re.findall(r'20\d{2}', ' '.join(v for k, v in specs if k == '発売日'))
    year = int(years[0]) if len(set(years)) == 1 else None
    catalog['release_year'] = year
    retailer, level, offers, round_label = None, 0, [], ''
    for node in root.walk():
        if re.fullmatch('h[1-6]', node.tag):
            nlevel = int(node.tag[1])
            if retailer and nlevel <= level:
                retailer = None
                round_label = ''
            if nlevel in (2, 3):
                text = node.text()
                if 'ポケモンセンターオンライン' in text:
                    retailer, level = 'ポケモンセンターオンライン', nlevel
                elif 'Amazon' in text:
                    retailer, level = 'Amazon', nlevel
            if retailer and nlevel > level:
                round_label = node.text() if re.search(r'\d+回目|追加|再抽選', node.text()) else ''
        if node.tag != 'table' or not retailer:
            continue
        rows = dict(table_rows(node))
        sale = rows.get('販売形式', '')
        mode = 'lottery' if re.search('抽選|招待', sale) else 'sale' if re.search('再販|再入荷|追加販売|販売再開', sale) else None
        if not mode:
            continue
        quantity = rows.get('販売数量', '') + rows.get('購入制限', '')
        if ('パック' in quantity and not re.search('BOX|ボックス', quantity, re.I)) or re.search('箱なし|箱無し', quantity):
            continue
        period = next((rows[k] for k in ('応募受付期間', '抽選応募期間', '申込受付期間', '受付期間') if k in rows), '') if mode == 'lottery' else rows.get('販売期間', '')
        start, end = date_range(period, year)
        status_text = rows.get('販売状況', '')
        status = 'closed' if re.search('終了|売り切れ|在庫なし|在庫切れ', status_text + period) else 'open' if re.search('受付中|販売中|在庫あり', status_text) else 'unknown'
        links = [a.attrs.get('href', '') for a in node.walk('a')]
        urls = [canonical_product(u) for u in links]
        urls = [u for u in urls if u and (('amazon.co.jp' in u) == (retailer == 'Amazon'))]
        official = next((u for u in links if urlsplit(u).hostname in {'www.pokemoncenter-online.com', 'www.support.pokemoncenter-online.com'}), '')
        item = observation(title, retailer, mode, 'ポケゲトちゃんねる', post['link'],
                           urls[0] if urls else official, status, start, end, period=period)
        item['round_label'] = round_label
        item['tentative'] = bool(re.search('予想|見込み|未定', period))
        item['application_deadline'] = bool(mode == 'lottery' and period and end and not start)
        weekdays = re.findall(r'\d{1,2}月\d{1,2}日\(([月火水木金土日])\)', period)
        parsed = [datetime.fromisoformat(d) for d in (start, end) if d]
        item['calendar_error'] = bool(weekdays and (len(weekdays) != len(parsed) or any('月火水木金土日'[d.weekday()] != day for d, day in zip(parsed, weekdays))))
        item['start_precision'] = 'minute' if re.search(r'\d{1,2}(?:時|:)', re.split('[~〜～]', period, maxsplit=1)[0]) else 'day'
        offers.append(item)
    return offers, catalog


def mirar_offers(html):
    root = Tree(html).root
    main = next((n for n in root.walk() if n.has_class('cps-post-main')), None)
    if not main:
        raise ValueError('Mirar layout changed')
    offers = []
    found = False
    for table in main.walk('table'):
        rows = table_rows(table)
        if not rows or rows[0][0] != '商品名' or 'Amazon' not in rows[0][1]:
            continue
        found = True
        for title, value in rows[1:]:
            if product_kind(title, '') not in {'box', 'box_set'}:
                continue
            status = 'closed' if re.search('受付終了|出品なし|他の出品者のみ', value) else 'open' if '受付中' in value else 'unknown'
            offers.append(observation(title, 'Amazon', 'lottery', *SOURCES['mirar'], status=status))
    if not found:
        raise ValueError('Mirar status tables missing')
    return offers


def board_offers(html, known):
    root = Tree(html).root
    section = root.by_id('amazon-invite')
    if not section:
        raise ValueError('Amazon invitation list missing')
    offers = []
    for a in section.walk('a'):
        if not a.has_class('azi'):
            continue
        name = next(a.walk('b'), None)
        title = name.text() if name else ''
        url = canonical_product(a.attrs.get('href', ''))
        # This site mixes TCGs. A separate Pokémon source must identify the box.
        if identity(title) not in known or not url:
            continue
        if product_kind(title, '') not in {'box', 'box_set'}:
            continue
        offers.append(observation(title, 'Amazon', 'lottery', *SOURCES['board'], url, 'open'))
    return offers


def cardchusen_offers(html, known):
    root = Tree(html).root
    cards = [n for n in root.walk('article') if 'data-card' in n.attrs]
    if not cards:
        raise ValueError('Cardchusen cards missing')
    offers = []
    for card in cards:
        if card.attrs.get('data-category') != 'pokemon':
            continue
        store = next((n for n in card.walk() if n.has_class('board-card__store')), None)
        title = store.attrs.get('title', '') if store else ''
        if identity(title) not in known:
            continue
        cta = next((a for a in card.walk('a') if a.has_class('board-card__cta')), None)
        url = canonical_product(cta.attrs.get('href', '')) if cta else None
        host = urlsplit(url or '').hostname
        if host == 'www.amazon.co.jp' and card.attrs.get('data-store') == 'amazon-jp':
            retailer = 'Amazon'
        elif host == 'www.pokemoncenter-online.com' and store and 'オンライン' in store.text():
            retailer = 'ポケモンセンターオンライン'
        else:
            continue
        # Listing presence (which includes upcoming rounds) is discovery only.
        # Explicit dates in a future adapter or another source prove open status.
        offers.append(observation(title, retailer, 'lottery', *SOURCES['cardchusen'], url))
    return offers


def plan_events(state, offers, now, successful_sources):
    """Notify each eligible round once, including first discovery during its window."""
    tracked = state.setdefault('events', {})
    initialized = set(state.setdefault('initialized_sources', []))
    aliases = {}
    for offer in offers:
        url = canonical_product(offer['url'])
        if url:
            aliases.setdefault((offer['retailer'], offer['product']), url)
    grouped = {}
    for offer in offers:
        o = dict(offer)
        if not o.get('direct'):
            o['url'] = aliases.get((o['retailer'], o['product']), o['url'])
        key = '{}:{}:{}'.format(o['retailer'], o['product'], o['kind'])
        grouped.setdefault(key, []).append(o)
    pending, unresolved = [], []
    for key, group in grouped.items():
        old = tracked.get(key)
        definite = [o for o in group if at_time(o, now) not in {'unknown', 'deadline_known'}]
        direct = [o for o in definite if o.get('direct')]
        if direct:
            definite = direct
        opened = [o for o in definite if at_time(o, now) == 'open']
        qualified = [o for o in opened if o.get('direct') or (o.get('start') and o.get('end'))]
        if not qualified and len({o['source'] for o in opened}) >= 2:
            qualified = opened
        deadlines = [o for o in group if at_time(o, now) == 'deadline_known']
        # A full official interval, even when scheduled/closed, outranks all summaries.
        chosen = None
        if direct:
            chosen = max(direct, key=lambda o: (at_time(o, now) == 'open', o.get('start') or ''))
        elif qualified:
            chosen = max(qualified, key=lambda o: o.get('start') or '')
        elif deadlines:
            chosen = max(deadlines, key=lambda o: o['end'])
        elif definite and not opened:
            chosen = max(definite, key=lambda o: (at_time(o, now) == 'scheduled', o.get('start') or ''))
        if chosen is None:
            if any(o['kind'] == 'lottery' and (not o.get('end') or datetime.fromisoformat(o['end']) >= now) for o in group):
                unresolved.append({'title': group[0]['title'], 'retailer': group[0]['retailer'],
                                   'reason': '日付と曜日が一致せず年を確定できません' if any(o.get('calendar_error') for o in group) else '応募期間・受付状態を確定できません', 'source_url': group[0]['source_url']})
            continue
        chosen = dict(chosen)
        # Carry round names from the matching summary without altering product identity.
        if not chosen.get('round_label'):
            chosen['round_label'] = next((o.get('round_label', '') for o in group
                                          if o.get('end') == chosen.get('end') and o.get('round_label')), '')
        status = at_time(chosen, now)
        active_sources = {o['source'] for o in qualified}
        established = bool((active_sources or {chosen['source']}) & initialized)
        is_deadline = status == 'deadline_known'
        new_round = bool(old and old.get('end') and chosen.get('start') and chosen['start'] > old['end'])
        delivered_end = old.get('notified_end') if old else None
        if old and not delivered_end and (old.get('notified') or old.get('status') == 'open'):
            delivered_end = old.get('end')
        same_delivered_round = bool(delivered_end and delivered_end == chosen.get('end'))
        if delivered_end and chosen.get('start') and chosen['start'] > delivered_end:
            new_round = True
        opening = status == 'open' and not same_delivered_round and (
            (old and (old['status'] == 'scheduled' or new_round or
                      (old['status'] == 'closed' and not chosen.get('start')) or
                      (old['status'] in {'unknown', 'deadline_known'} and not delivered_end and not old.get('notified')))) or
            (not old and (chosen.get('direct') or
                          (established and (len(active_sources & initialized) >= 2 or
                           (chosen.get('start') and datetime.fromisoformat(chosen['start']) >
                            datetime.fromisoformat(state['started_at']).replace(tzinfo=timezone.utc))))))
        )
        if chosen['kind'] == 'sale' and not old and not chosen.get('start'):
            opening = False
        deadline_notice = is_deadline and established and not same_delivered_round and (
            not old or (not old.get('baseline') and not old.get('notified') and not delivered_end) or
            (old.get('end') and chosen['end'] > old['end'] and old.get('status') == 'closed' and
             chosen.get('round_label') and chosen.get('round_label') != old.get('round_label')))
        after = dict(status=status, start=chosen.get('start'), end=chosen.get('end'),
                     title=chosen['title'], last_observed=now.isoformat(),
                     notified=bool(same_delivered_round), notified_end=delivered_end,
                     round_label=chosen.get('round_label', ''), source=chosen['source'],
                     source_url=chosen['source_url'], period=chosen.get('period', ''),
                     first_seen_at=old.get('first_seen_at', now.isoformat()) if old else now.isoformat())
        if opening or deadline_notice:
            event = '抽選受付情報' if deadline_notice else '抽選開始' if chosen['kind'] == 'lottery' else '再販・在庫復活'
            sources = sorted({chosen['source']} if chosen.get('direct') or deadline_notice else active_sources)
            if deadline_notice:
                period = '受付期限: ' + datetime.fromisoformat(chosen['end']).astimezone(JST).strftime('%Y/%m/%d %H:%M JST') + '（開始日時未確認）'
            elif chosen.get('start') and chosen.get('end'):
                period = '応募受付: ' + datetime.fromisoformat(chosen['start']).astimezone(JST).strftime('%Y/%m/%d %H:%M') + '〜' + datetime.fromisoformat(chosen['end']).astimezone(JST).strftime('%Y/%m/%d %H:%M JST')
            else:
                period = '受付: ' + chosen.get('period', '受付中の掲載を確認')
            evidence = '公式の応募受付期間を確認' if chosen.get('direct') and chosen['kind'] == 'lottery' else '販売店ページで確認' if chosen.get('direct') else '掲載情報: ' + '、'.join(sources)
            note = evidence + '\n' + period
            if chosen.get('round_label'):
                note += '\n受付回: ' + chosen['round_label']
            note += '\n[情報元](' + chosen['source_url'] + ')\n販売単位・受付状況はリンク先で確認してください。'
            after['notified'] = True
            after['notified_end'] = chosen.get('end')
            display_title = chosen['title'] + ('（' + chosen['round_label'] + '）' if chosen.get('round_label') else '')
            pending.append(dict(chosen, title=display_title, key=key, event=event, note=note, after=after, source='、'.join(sources)))
        else:
            if not old and status in {'open', 'deadline_known'} and not established and not chosen.get('direct'):
                after['baseline'] = True
            tracked[key] = after
    state['lottery_unresolved'] = unresolved
    state['initialized_sources'] = sorted(initialized | set(successful_sources))
    return pending
