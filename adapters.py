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
    if any(x in title for x in ("空箱", "収納", "デッキケース", "スリーブ", "オリパ", "コレクターボックス", "コレクターBOX")):
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
