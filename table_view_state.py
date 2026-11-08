"""Presentation-only preferences. Never alter source rows or forecast archives."""
from collections.abc import Mapping
from math import isfinite

import pandas as pd


VERSION = 1


def _height(value, fallback=None):
    """Accept only presentation sizing; booleans and arbitrary CSS stay invalid."""
    if value is None or isinstance(value, str) and value == 'content':
        return value
    if type(value) is int and 200 <= value <= 5000:
        return value
    return fallback


def normalize_view(schema, defaults=None, supplied=None):
    """Reconcile a saved view with today's columns, discarding invalid settings."""
    schema = list(dict.fromkeys(str(name) for name in schema))
    known = set(schema)

    def clean(value, base):
        if not isinstance(value, Mapping) or value.get('version') != VERSION:
            value = {}
        order = value.get('columns', [])
        order = order if isinstance(order, list) else []
        order = list(dict.fromkeys(name for name in order if isinstance(name, str) and name in known))
        previous = set(order)
        order += [name for name in base['columns'] if name not in previous]
        hidden = value.get('hidden', [])
        hidden = hidden if isinstance(hidden, list) else []
        hidden = {name for name in hidden if isinstance(name, str) and name in known}
        hidden.update(name for name in base['hidden'] if name not in previous)
        widths = dict(base['widths'])
        incoming = value.get('widths', {})
        if isinstance(incoming, Mapping):
            widths.update({name: width for name, width in incoming.items()
                           if name in known and type(width) in (int, float) and isfinite(width)
                           and width == int(width) and 40 <= width <= 2000})
        sort, seen = [], set()
        incoming = value.get('sort', base['sort'])
        if isinstance(incoming, list):
            for item in incoming:
                if (isinstance(item, Mapping) and isinstance(item.get('column'), str)
                        and item['column'] in known and item['column'] not in seen
                        and item.get('direction') in ('asc', 'desc')):
                    sort.append(dict(column=item['column'], direction=item['direction']))
                    seen.add(item['column'])
        return dict(version=VERSION, columns=order, hidden=[name for name in order if name in hidden],
                    widths=widths, sort=sort, height=_height(value.get('height', base['height']), base['height']))

    empty = dict(version=VERSION, columns=schema, hidden=[], widths={}, sort=[], height=None)
    base = clean(defaults, empty)
    return clean(supplied, base) if supplied is not None else base


def apply_dataframe_view(frame, view):
    """Stable sort a copy using original types; retain every source column/index."""
    result = frame.copy(deep=True)
    state = normalize_view([str(name) for name in result.columns], supplied=view)
    mapping = {str(name): name for name in result.columns}
    try:
        # Stable passes, least significant first, retain original order on ties.
        for item in reversed(state['sort']):
            result = result.sort_values(mapping[item['column']], ascending=item['direction'] == 'asc',
                                        kind='stable', na_position='last')
    except (TypeError, ValueError, KeyError):
        # Mixed objects must not break the page or silently acquire a fake order.
        return frame.copy(deep=True)
    return result
