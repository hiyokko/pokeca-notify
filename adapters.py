"""Extract only product details and the two requested retailers, not adverts."""
import hashlib
import json
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from htmltools import Tree, clean, table_rows

SITE = "https://pokemon-infomation.com"
OFFICIAL = "https://www.pokemon-card.com/info/"
RETAILERS = ("Amazon", "ポケモンセンターオンライン")
ACTION = re.compile(r"抽選|招待|予約|再販|再入荷|追加販売|受注|販売再開|在庫復活")
BOX = re.compile(r"(?:\d+\s*)?BOX|ボックス", re.I)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def product_kind(title, product_details):
    """Only title and product-spec table are evidence. Sidebar ads don't count."""
    title, details = clean(title), clean(product_details)
    if any(x in title for x in ("空箱", "収納", "デッキケース", "スリーブ", "オリパ")):
        return "excluded"
    if any(x in title for x in ("ルリナセット", "イーブイズセット", "ブイズセット")):
        return "box_set"
    if "セット" in title:
        # A few loose packs in a set are not a full expansion box.
        return "box_set" if BOX.search(details) else "review"
    if any(x in title for x in ("スターターデッキ", "スタートデッキ", "構築デッキ")):
        return "excluded"
    if BOX.search(title + " " + details):
        return "box"
    return "review"


def canonical_product(url):
    p = urlsplit(url)
    if p.scheme != "https":
        return None
    if p.hostname in {"www.amazon.co.jp", "amazon.co.jp"}:
        m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", p.path + "/", re.I)
        return "https://www.amazon.co.jp/dp/" + m[1].upper() if m else None
    if p.hostname == "www.pokemoncenter-online.com" and re.search(r"/(?:product|item|goods)/", p.path):
        return urlunsplit(("https", p.hostname, p.path, p.query, ""))
    return None


def article_records(post):
    root = Tree(post["content"]["rendered"]).root
    title = Tree(post["title"]["rendered"]).root.text()
    # Select tables explicitly describing this product, never recommendation blocks.
    specs = []
    for table in root.walk("table"):
        rows = table_rows(table)
        if any(k == "商品名" for k, _ in rows):
            specs.extend(rows)
    kind = product_kind(title, " ".join(k + " " + v for k, v in specs))
    if kind not in {"box", "box_set"} or not ACTION.search(title):
        return [], {"title": title, "url": post["link"], "kind": kind}
    nodes = list(root.walk())
    records = []
    for i, heading in enumerate(nodes):
        if heading.tag not in {"h2", "h3"}:
            continue
        text = heading.text()
        retailer = next((r for r in RETAILERS if r in text), None)
        if not retailer or not ACTION.search(text):
            continue
        level = int(heading.tag[1])
        section = []
        for node in nodes[i + 1:]:
            if re.fullmatch(r"h[1-6]", node.tag) and int(node.tag[1]) <= level:
                break
            section.append(node)
        rows = [row for n in section if n.tag == "table" for row in table_rows(n)]
        rows = [(k, v) for k, v in rows if k in {
            "商品", "販売形式", "受付期間", "受付期限", "予約期間", "販売期間", "当選発表",
            "購入期間", "応募条件", "販売数量", "購入制限", "販売価格", "応募方法", "販売状況"}]
        # Avoid notifying an explicitly loose-pack offer even for a box product.
        quantities = " ".join(v for k, v in rows if k in {"販売数量", "購入制限"})
        if "パック" in quantities and not BOX.search(quantities):
            continue
        if not rows:
            continue
        urls = sorted({u for n in section if n.tag == "a"
                       for u in [canonical_product(n.attrs.get("href", ""))] if u})
        relevant_links = sorted({n.attrs.get("href", "") for n in section if n.tag == "a"
                                 and urlsplit(n.attrs.get("href", "")).hostname in
                                 {"www.amazon.co.jp", "amazon.co.jp", "www.pokemoncenter-online.com"}})
        key = "article:{}:{}".format(post["id"], retailer)
        records.append({"key": key, "title": title, "retailer": retailer, "kind": kind,
                        "source": "ポケゲトちゃんねる", "url": post["link"],
                        "signature": fingerprint([rows, relevant_links]),
                        "products": urls,
                        "note": "対象店舗の抽選・予約・再販欄に更新。受付状況・販売単位はリンク先で確認。"})
    return records, {"title": title, "url": post["link"], "kind": kind}


def official_records(html):
    root = Tree(html).root
    news = root.by_id("newsTab_all")
    if not news:
        raise ValueError("公式ニュースの一覧を検出できません")
    records = {}
    for a in news.walk("a"):
        if not a.has_class("List_item_inner"):
            continue
        img = next(a.walk("img"), None)
        title = clean(img.attrs.get("alt", "") if img else a.text())
        # Generic '商品の抽選' notices have no box evidence; don't claim a match.
        if not ACTION.search(title) or product_kind(title, title) not in {"box", "box_set"}:
            continue
        url = urljoin(OFFICIAL, a.attrs.get("href", "").strip())
        if urlsplit(url).hostname not in {"www.pokemon-card.com", "www.pokemoncenter-online.com"}:
            continue
        dates = [n.text() for n in a.walk() if n.has_class("Date")]
        key = "official:" + url
        records[key] = {"key": key, "title": title, "url": url,
                        "retailer": "ポケモンセンターオンライン", "source": "ポケモンカード公式",
                        "signature": fingerprint([title, dates]), "products": [],
                        "note": "BOX関連の抽選・追加販売のお知らせ。対象店舗・商品は公式記事で確認。"}
    return list(records.values())


def stock_status(html, url):
    """Conservative Amazon adapter: unknown never becomes out-of-stock."""
    root = Tree(html).root
    if urlsplit(url).hostname != "www.amazon.co.jp":
        return "unknown"
    if root.by_id("captchacharacters") or not root.by_id("productTitle"):
        return "unknown"
    availability = root.by_id("availability")
    text = availability.text() if availability else ""
    if any(x in text for x in ("現在在庫切れ", "一時的に在庫切れ", "現在お取り扱いできません")):
        return "unavailable"
    seller = root.by_id("sellerProfileTriggerId")
    if not seller:
        return "unknown"
    if seller.text() != "Amazon.co.jp":
        return "unavailable"
    button = root.by_id("add-to-cart-button") or root.by_id("buy-now-button")
    if button and "disabled" not in button.attrs and any(x in text for x in ("在庫あり", "残り", "予約受付中")):
        return "available"
    return "unknown"
