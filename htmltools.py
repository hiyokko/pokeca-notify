"""Small HTML tree used by the site adapters (standard library only)."""
import re
import unicodedata
from html.parser import HTMLParser


def clean(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


class Node:
    def __init__(self, tag="root", attrs=None, parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs or []), parent
        self.children = []

    def text(self):
        if self.tag in {"script", "style", "noscript"}:
            return ""
        return clean(" ".join(c if isinstance(c, str) else c.text() for c in self.children))

    def walk(self, tag=None):
        if tag is None or self.tag == tag:
            yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk(tag)

    def by_id(self, value):
        return next((n for n in self.walk() if n.attrs.get("id") == value), None)

    def has_class(self, name):
        return name in self.attrs.get("class", "").split()


class Tree(HTMLParser):
    VOID = set("area base br col embed hr img input link meta param source track wbr".split())

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def table_rows(table):
    rows = []
    for row in table.walk("tr"):
        cells = [n for n in row.children if isinstance(n, Node) and n.tag in {"th", "td"}]
        if len(cells) >= 2:
            rows.append((cells[0].text(), " / ".join(n.text() for n in cells[1:])))
    return rows
