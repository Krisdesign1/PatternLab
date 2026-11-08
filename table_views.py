"""Saved per-browser table views through an explicit, public CCv2 editor.

Native Streamlit column-menu gestures expose no view-state event. Only settings
saved through this editor are durable. This layer owns presentation, not data.
"""
from collections.abc import Mapping
from copy import deepcopy
from hashlib import sha256
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from threading import local

import pandas as pd
import streamlit as st
from streamlit.errors import StreamlitAPIException

from table_view_state import apply_dataframe_view, normalize_view


_ROOT = Path(__file__).parent
_RENDER = local()


def begin_render():
    """Reset ephemeral instance numbering on full-page reruns only."""
    _RENDER.instances = {}


@st.cache_resource(show_spinner=False)
def _editor():
    return st.components.v2.component(
        'patternlab_table_view',
        html='<div class="pl-table-view"></div>',
        css=(_ROOT / 'table_view_editor.css').read_text(),
        js=(_ROOT / 'table_view_editor.js').read_text(),
    )


def storage_key(table_id):
    if not isinstance(table_id, str) or not table_id:
        raise ValueError('A stable logical table ID is required.')
    return 'patternlab:table-view:v1:' + sha256(table_id.encode()).hexdigest()[:32]


def _frame(data):
    if isinstance(data, pd.DataFrame):
        return data
    if hasattr(data, 'data') and isinstance(data.data, pd.DataFrame):
        return data.data
    if isinstance(data, pd.Series):
        return data.to_frame()
    if hasattr(data, 'to_pandas'):
        return data.to_pandas()
    return pd.DataFrame(data)


def _defaults(frame, kwargs):
    schema = [str(name) for name in frame.columns]
    config = kwargs.get('column_config') or {}
    order = kwargs.get('column_order')
    order = list(order) if order is not None else schema
    hidden = [str(name) for name in frame.columns if name in config and config[name] is None]
    hidden += [name for name in schema if name not in order]
    widths = {str(name): value['width'] for name, value in config.items()
              if isinstance(value, Mapping) and type(value.get('width')) is int}
    return normalize_view(schema, supplied=dict(version=1, columns=[str(name) for name in order],
                                                hidden=hidden, widths=widths, sort=[]))


def _columns(frame, config):
    values = []
    for name in frame.columns:
        setting = config.get(name)
        label = setting.get('label') if isinstance(setting, Mapping) else setting if isinstance(setting, str) else None
        values.append(dict(id=str(name), label=label or str(name)))
    return values


def _noop():
    pass


def _control(columns, *, table_id, key, defaults, reset_token=None, rich_table_html=None):
    identity = storage_key(table_id)
    instance = identity + ':' + str(key or '')
    editor_key = 'table_view_' + sha256(instance.encode()).hexdigest()[:24]
    previous = st.session_state.get(editor_key) or {}
    supplied = previous.get('view') if isinstance(previous, Mapping) else None
    current = normalize_view([item['id'] for item in columns], defaults, supplied)
    data = dict(storage_key=identity, columns=columns, defaults=defaults, current=current,
                reset_token=str(reset_token) if reset_token is not None else None)
    if rich_table_html is not None:
        data['rich_table_html'] = rich_table_html
    try:
        result = _editor()(data=data, key=editor_key, on_view_change=_noop)
    except StreamlitAPIException as error:
        # AppTest/runtime replacement can retain a cached callable after its
        # registry disappears. Re-register through the public API, once.
        if str(error) != "Component 'patternlab_table_view' is not registered.":
            raise
        _editor.clear()
        result = _editor()(data=data, key=editor_key, on_view_change=_noop)
    value = getattr(result, 'view', None)
    return normalize_view([item['id'] for item in columns], defaults, value if value is not None else supplied)


def _view_kwargs(frame, view, kwargs):
    result = dict(kwargs)
    mapping = {str(name): name for name in frame.columns}
    result['column_order'] = [mapping[name] for name in view['columns'] if name not in view['hidden']]
    config = deepcopy(result.get('column_config') or {})
    for name in view['columns']:
        original = mapping[name]
        value = config.get(original)
        if original in config and value is None and name not in view['hidden']:
            config.pop(original)
        if name in view['widths']:
            value = config.get(original)
            config[original] = dict(value, width=view['widths'][name]) if isinstance(value, Mapping) else dict(
                label=value if isinstance(value, str) else None, width=view['widths'][name])
    result['column_config'] = config
    return result


@st.fragment
def _dataframe_fragment(data, table_id, reset_token, instance, kwargs):
    frame = _frame(data)
    # Duplicate labels have no unambiguous column identity; retain native output.
    if len(set(map(str, frame.columns))) != len(frame.columns):
        return st.dataframe(data, **kwargs)
    defaults = _defaults(frame, kwargs)
    view = _control(_columns(frame, kwargs.get('column_config') or {}), table_id=table_id,
                    key=instance, defaults=defaults, reset_token=reset_token)
    rendered = apply_dataframe_view(frame, view)
    if hasattr(data, 'data') and isinstance(data.data, pd.DataFrame):
        # Existing styles are evaluated on the original frame; don't discard them.
        rendered = data if not view['sort'] else rendered.style.use(data.export())
    return st.dataframe(rendered, **_view_kwargs(frame, view, kwargs))


def _instance(table_id, key=None):
    if key is not None:
        return str(key)
    counts = getattr(_RENDER, 'instances', {})
    value = counts.get(table_id, 0)
    counts[table_id] = value + 1
    _RENDER.instances = counts
    return str(value)


def dataframe(data=None, *, table_id, reset_token=None, view_key=None, **kwargs):
    """Native table plus explicit saved view, isolated from page computation."""
    # Interactive row actions must keep their original index/return semantics.
    if kwargs.get('on_select', 'ignore') != 'ignore':
        return st.dataframe(data, **kwargs)
    instance = _instance(table_id, view_key if view_key is not None else kwargs.get('key'))
    return _dataframe_fragment(data, table_id, reset_token, instance, kwargs)


class _Headers(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_head = False
        self.in_cell = False
        self.headers = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == 'thead':
            self.in_head = True
        elif self.in_head and tag in ('th', 'td'):
            self.in_cell = True
            self.parts = []

    def handle_data(self, data):
        if self.in_head and self.in_cell:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag in ('th', 'td') and self.in_head and self.in_cell:
            self.headers.append(' '.join(' '.join(self.parts).split()))
            self.in_cell = False
        elif tag == 'thead':
            self.in_head = False


@st.fragment
def _html_fragment(markup, table_id, instance):
    """Preserve rich scenario cells while exposing the same saved-view editor."""
    parser = _Headers()
    parser.feed(markup)
    columns = [dict(id=name or f'Colonne {i + 1}', label=name or f'Colonne {i + 1}')
               for i, name in enumerate(parser.headers)]
    defaults = normalize_view([item['id'] for item in columns])
    _control(columns, table_id=table_id, key=instance, defaults=defaults, rich_table_html=markup)


def html_table(markup, *, table_id, key=None):
    return _html_fragment(markup, table_id, _instance(table_id, key))


def _markdown_cell(value):
    text = escape(str(value))
    palette = {'blue': '#8fc1ff', 'violet': '#d7b9ff', 'orange': '#ffc58a',
               'green': '#b6e4cc', 'red': '#ffb5b5', 'gray': '#c2cee0'}
    for color, background in palette.items():
        text = re.sub(r':' + color + r'-badge\[([^\]]*)\]',
                      lambda match: '<span class="pl-view-badge" style="background:' + background +
                      ';color:#101c30">' + match.group(1) + '</span>', text)
    return text.replace('\n', '<br>')


def table(data=None, *, table_id, **kwargs):
    """Static Markdown tables retain badges and gain the same view preferences."""
    frame = _frame(data)
    head = ''.join('<th>' + escape(str(name)) + '</th>' for name in frame.columns)
    body = ''.join('<tr>' + ''.join('<td>' + _markdown_cell(value) + '</td>' for value in row) + '</tr>'
                   for row in frame.itertuples(index=False, name=None))
    markup = '<div class="pl-view-rich"><table><thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table></div>'
    return html_table(markup, table_id=table_id)
