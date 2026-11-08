"""Read application HTML from native and CCv2 renderers in headless UI tests."""
from html.parser import HTMLParser
import json
import re
from types import SimpleNamespace

import pandas as pd


def html_nodes(at):
    values = list(at.get('html'))
    for item in at.get('bidi_component'):
        markup = json.loads(item.proto.json).get('rich_table_html')
        if markup:
            values.append(SimpleNamespace(proto=SimpleNamespace(body=markup)))
    return values


class _Table(HTMLParser):
    def __init__(self):
        super().__init__()
        self.headers, self.rows, self.row, self.cell = [], [], [], None
        self.head = False

    def handle_starttag(self, tag, attrs):
        if tag == 'thead': self.head = True
        if tag == 'tr': self.row = []
        if tag in ('th', 'td'): self.cell = []

    def handle_data(self, data):
        if self.cell is not None: self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ('th', 'td') and self.cell is not None:
            self.row.append(''.join(self.cell)); self.cell = None
        if tag == 'tr':
            if self.head: self.headers = self.row
            else: self.rows.append(self.row)
        if tag == 'thead': self.head = False


def static_frames(at):
    frames = []
    for item in html_nodes(at):
        if 'class="pl-view-rich"' in item.proto.body:
            parser = _Table(); parser.feed(item.proto.body)
            frames.append(pd.DataFrame(parser.rows, columns=parser.headers))
    return frames


def badge_text(value):
    return re.sub(r":(?:blue|violet|orange|green|red|gray)-badge\[([^\]]*)\]", r"\1", value)
