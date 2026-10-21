"""Readable, bounded scenario tables from saved prediction payloads only.

The union of the methods' saved top three is displayed once. Probabilities are
never rescaled and never recomputed from a current model or later match data.
"""
from collections.abc import Mapping
from html import escape
import hashlib
import json
from math import isclose, isfinite

import streamlit as st

from coupon_sizes import validate_size
from league_model import VALUES
from scenario_odds import DEFAULT_ODDS, format_odds, scenario_odds_text
import prospective_models as prospective


METHOD_ORDER = ('frequency', 'clubs', 'combined', 'catboost', 'context_neighbors')
METHOD_LABELS = {'frequency': 'Répétitions', 'clubs': 'Clubs', 'combined': 'Combinaison',
                 'catboost': 'CatBoost', 'context_neighbors': 'Contextes'}
TOKEN_CLASSES = {'1st': 'first', '2nd': 'second', 'Égal': 'equal'}
TOKEN_MEANINGS = {'1st': 'Plus de buts avant la pause', '2nd': 'Plus de buts après la pause',
                  'Égal': 'Autant de buts dans chaque période'}
PAGE_SIZE = 10
OUTCOME_FILTERS = {'all': 'Tous les verdicts', 'hit': 'Scénario réalisé',
                   'miss': 'Aucun scénario proposé réalisé', 'pending': 'Scénarios en attente',
                   'void': 'Groupe annulé', 'unresolved': 'Scénarios à vérifier',
                   'unavailable': 'Vérification indisponible'}


def _sequence(value, *, size=None):
    if not isinstance(value, (list, tuple)):
        raise ValueError('Le scénario doit conserver une liste ordonnée de résultats.')
    sequence = tuple(value)
    validate_size(len(sequence))
    if (size is not None and len(sequence) != size) or any(item not in VALUES for item in sequence):
        raise ValueError('Les scénarios doivent conserver les mêmes matchs et les trois résultats reconnus.')
    return sequence


def sequence_html(sequence):
    """Return escaped badges, with labels and positions remaining readable.

    Unknown labels are escaped as plain text; matrix input is validated more
    strictly. This helper can also render a single match's observed result.
    """
    if not isinstance(sequence, (list, tuple)) or not 1 <= len(sequence) <= 20:
        raise ValueError('Une séquence doit contenir de 1 à 20 résultats ordonnés.')
    pieces = []
    for position, value in enumerate(sequence, 1):
        label = str(value)
        css = TOKEN_CLASSES.get(label, 'unknown')
        meaning = TOKEN_MEANINGS.get(label, 'Résultat non reconnu')
        title = escape(f'Match {position} : {meaning}', quote=True)
        badge = (f'<span class="pl-scenario-token pl-scenario-token-{css}" title="{title}">'
                 f'<span class="pl-scenario-position">{position}</span>{escape(label)}</span>')
        if pieces:
            pieces.append('<span class="pl-scenario-arrow" aria-hidden="true">→</span>')
        pieces.append(badge)
    readable = escape(' → '.join(str(value) for value in sequence), quote=True)
    return f'<span class="pl-scenario-sequence" aria-label="{readable}">{"".join(pieces)}</span>'


def build_matrix(methods, *, actual=None):
    """Build at most 15 distinct rows and five method columns, without I/O.

    Rows sort by the best saved rank, then the rank in each canonical method,
    then the sequence itself. A full saved distribution scores even another
    method's choice. Ranking-only payloads leave such cells unavailable.
    """
    if not isinstance(methods, Mapping) or not methods:
        return {'methods': [], 'rows': [], 'actual': None, 'actual_in_choices': None}
    if len(methods) > len(METHOD_ORDER) or any(not isinstance(name, str) for name in methods):
        raise ValueError('Le tableau accepte au maximum cinq méthodes nommées.')
    names = [name for name in METHOD_ORDER if name in methods] + sorted(set(methods) - set(METHOD_ORDER))
    saved, size, choices = {}, None, set()
    for name in names:
        payload = methods[name]
        ranking = payload.get('ranking') if isinstance(payload, Mapping) else None
        if not isinstance(ranking, list) or not ranking:
            raise ValueError('Chaque méthode doit conserver son classement avant match.')
        ranked = {}
        for rank, item in enumerate(ranking[:3], 1):
            if not isinstance(item, Mapping):
                raise ValueError('Le classement contient un scénario invalide.')
            sequence = _sequence(item.get('sequence'), size=size)
            size = size or len(sequence)
            probability = item.get('probability')
            if (isinstance(probability, bool) or not isinstance(probability, (int, float))
                    or not isfinite(probability) or not 0 <= probability <= 1):
                raise ValueError('Une probabilité enregistrée est absente ou invalide.')
            if sequence in ranked:
                raise ValueError('Le même scénario ne peut pas occuper deux rangs dans une méthode.')
            ranked[sequence] = (rank, float(probability))
            choices.add(sequence)
        full_distribution = payload.get('distribution') is not None
        if full_distribution:
            # Validate once before scoring the bounded union; never fall back
            # silently to ranking-only if a saved distribution is damaged.
            prospective.validate_method(payload, size)
        saved[name] = (payload, ranked, full_distribution)
    actual_sequence = _sequence(actual, size=size) if actual is not None else None

    def order(sequence):
        ranks = tuple(saved[name][1].get(sequence, (4, None))[0] for name in names)
        return min(ranks), ranks, sequence

    rows = []
    for sequence in sorted(choices, key=order):
        cells = {}
        for name in names:
            payload, ranked, full_distribution = saved[name]
            rank, probability = ranked.get(sequence, (None, None))
            if full_distribution:
                computed = prospective.probability_for_sequence(payload, sequence)
                if probability is not None and not isclose(probability, computed, rel_tol=1e-9, abs_tol=1e-12):
                    raise ValueError('La probabilité du choix contredit la distribution enregistrée.')
                probability = computed
            cells[name] = {'probability': probability, 'rank': rank,
                           'status': 'ranked' if rank else 'outside_top3' if full_distribution else 'unavailable'}
        rows.append({'sequence': sequence, 'cells': cells,
                     'actual': sequence == actual_sequence if actual_sequence is not None else None})
    return {'methods': [{'key': name, 'label': METHOD_LABELS.get(name, name)} for name in names],
            'rows': rows, 'actual': actual_sequence,
            'actual_in_choices': actual_sequence in choices if actual_sequence is not None else None}


def _styles(theme):
    # Selectors belong exclusively to this component. Native widgets and other
    # tables are unaffected; no CSS relies on Streamlit's internal DOM classes.
    dark = theme == 'dark'
    palette = ('#e4edfa', '#aebbd0', '#26364e', '#162238', '#12233c', '#8fc1ff', '#153c69',
               '#ffc58a', '#563714', '#d7b9ff', '#3a285a', '#b6e4cc', '#203d34') if dark else (
               '#17243a', '#4b5c73', '#d8e0ec', '#f3f6fb', '#f7faff', '#175da7', '#e5f0ff',
               '#934b08', '#fff0df', '#7040a0', '#f2e9ff', '#246546', '#e8f5ee')
    text, muted, border, header, selected, blue, blue_bg, orange, orange_bg, violet, violet_bg, green, green_bg = palette
    return f'''<style>
.pl-scenario-root {{ --pl-text:{text}; --pl-muted:{muted}; --pl-border:{border}; --pl-header:{header}; --pl-selected:{selected}; color:var(--pl-text); font-family:inherit; width:100%; }}
.pl-scenario-root .pl-scenario-sequence {{ display:inline-flex; flex-wrap:wrap; gap:.35rem; align-items:center; max-width:100%; }}
.pl-scenario-root .pl-scenario-token {{ display:inline-flex; align-items:center; gap:.32rem; padding:.29rem .58rem; border-radius:.45rem; font-size:.88rem; line-height:1.35; font-weight:650; white-space:nowrap; }}
.pl-scenario-root .pl-scenario-token-first {{ color:{blue}; background:{blue_bg}; }}
.pl-scenario-root .pl-scenario-token-second {{ color:{orange}; background:{orange_bg}; }}
.pl-scenario-root .pl-scenario-token-equal {{ color:{violet}; background:{violet_bg}; }}
.pl-scenario-root .pl-scenario-token-unknown {{ color:var(--pl-text); background:var(--pl-header); }}
.pl-scenario-root .pl-scenario-position {{ font-size:.65rem; font-weight:500; opacity:.8; }}
.pl-scenario-root .pl-scenario-arrow {{ color:var(--pl-muted); font-size:.85rem; }}
.pl-scenario-root .pl-scenario-scroll {{ overflow-x:auto; border:1px solid var(--pl-border); border-radius:.75rem; }}
.pl-scenario-root .pl-scenario-table {{ width:100%; border-collapse:collapse; font-size:.95rem; margin:0; table-layout:auto; }}
.pl-scenario-root .pl-scenario-table th, .pl-scenario-root .pl-scenario-table td {{ padding:1.05rem 1rem; border-bottom:1px solid var(--pl-border); vertical-align:middle; }}
.pl-scenario-root .pl-scenario-table thead th {{ background:var(--pl-header); font-size:.77rem; letter-spacing:.035em; text-transform:uppercase; color:var(--pl-muted); text-align:right; white-space:nowrap; }}
.pl-scenario-root .pl-scenario-table thead th:first-child {{ text-align:left; min-width:15rem; }}
.pl-scenario-root .pl-scenario-table tbody th {{ text-align:left; min-width:15rem; max-width:32rem; font-weight:500; }}
.pl-scenario-root .pl-scenario-table td {{ text-align:right; min-width:7.5rem; font-variant-numeric:tabular-nums; }}
.pl-scenario-root .pl-scenario-table tbody tr:last-child th, .pl-scenario-root .pl-scenario-table tbody tr:last-child td {{ border-bottom:0; }}
.pl-scenario-root .pl-scenario-table .pl-scenario-realized {{ background:var(--pl-selected); }}
.pl-scenario-root .pl-scenario-probability {{ font-weight:650; white-space:nowrap; }}
.pl-scenario-root .pl-scenario-meta {{ display:block; font-size:.875rem; margin-top:.32rem; font-weight:400; color:var(--pl-muted); }}
.pl-scenario-root .pl-scenario-success {{ display:inline-block; background:{green_bg}; color:{green}; padding:.28rem .5rem; border-radius:.35rem; font-size:.8rem; white-space:nowrap; }}
.pl-scenario-root .pl-scenario-status {{ color:var(--pl-muted); font-size:.8rem; white-space:nowrap; }}
.pl-scenario-root .pl-scenario-label {{ display:block; margin:0 0 .6rem; font-weight:600; }}
.pl-scenario-root .pl-scenario-legend {{ display:flex; flex-wrap:wrap; gap:.75rem 1.25rem; }}
.pl-scenario-root .pl-scenario-legend-item {{ display:flex; align-items:center; gap:.5rem; font-size:.875rem; color:var(--pl-muted); }}
.pl-scenario-root .pl-scenario-sr {{ position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip-path:inset(50%); white-space:nowrap; border:0; }}
</style>'''


def _theme():
    return 'dark' if st.context.theme.type == 'dark' else 'light'


def styles_html(theme=None):
    """Scoped CSS for callers composing their own scenario-only markup."""
    return _styles(_theme() if theme is None else theme)


def render_legend():
    """Render the same three semantic colors used throughout the matrices."""
    items = ''.join(f'<span class="pl-scenario-legend-item"><span class="pl-scenario-token '
                    f'pl-scenario-token-{TOKEN_CLASSES[value]}">{value} · {format_odds(DEFAULT_ODDS[value])}</span>'
                    f'{TOKEN_MEANINGS[value]}</span>'
                    for value in ('1st', '2nd', 'Égal'))
    return st.html(styles_html() + f'<div class="pl-scenario-root"><div class="pl-scenario-legend" '
                   f'aria-label="Légende des résultats par période">{items}</div></div>')


def render_sequence(sequence, *, label=None):
    title = f'<span class="pl-scenario-label">{escape(str(label))}</span>' if label else ''
    return st.html(_styles(_theme()) + f'<div class="pl-scenario-root">{title}{sequence_html(sequence)}</div>')


def render_outcome(outcome, *, show_sequence=False):
    """Make the frozen top-choice verdict visible without opening a matrix.

    The summary is computed from saved rankings and the final ordered outcome;
    a group's lifecycle state alone never supplies a success verdict.
    """
    if not outcome:
        return
    state = outcome['state']
    color, icon = {'hit': ('green', 'check_circle'), 'miss': ('red', 'cancel'),
                   'pending': ('blue', 'hourglass_empty'), 'void': ('gray', 'block'),
                   'unresolved': ('orange', 'help'), 'unavailable': ('orange', 'help')}[state]
    st.badge(outcome['label'], color=color, icon=f':material/{icon}:')
    if state == 'hit':
        st.caption(' · '.join(f"{METHOD_LABELS.get(match['method'], 'Méthode enregistrée')} · Choix {match['rank']}"
                             for match in outcome['matches']))
    if show_sequence and outcome.get('actual'):
        st.markdown('**Scénario réalisé :**')
        render_sequence(outcome['actual'])
    if show_sequence:
        st.caption('Une réussite exige tous les résultats exacts, dans le même ordre, parmi les choix enregistrés avant match. '
                   'Les résultats partiels ne donnent aucun verdict de réussite ou d’échec.')


def _percentage(value):
    if 0 < value * 100 < .01:
        return '&lt; 0,01 %'
    return f'{100 * value:.2f}'.replace('.', ',') + ' %'


def matrix_html(matrix, *, theme='dark', rows=None, title=None):
    """Generate a semantic HTML table; pure helper for visual/security tests."""
    caption = escape(str(title or 'Probabilités des scénarios par méthode avant match'))
    methods = matrix['methods']
    headers = '<th scope="col">Scénario</th><th scope="col">Cote cumulée</th>' + ''.join(
        f'<th scope="col">{escape(method["label"])}</th>' for method in methods)
    show_actual = matrix['actual'] is not None
    if show_actual:
        headers += '<th scope="col">Résultat</th>'
    body = []
    for row in matrix['rows'] if rows is None else rows:
        cells = [f'<th scope="row">{sequence_html(row["sequence"])}</th>']
        cells.append(f'<td><span class="pl-scenario-probability">{scenario_odds_text(row["sequence"])}</span>'
                     '<span class="pl-scenario-meta">Référence fixe</span></td>')
        for method in methods:
            cell = row['cells'][method['key']]
            if cell['probability'] is None:
                content = '<span class="pl-scenario-probability">—</span><span class="pl-scenario-meta">Non classé dans les 3</span>'
            else:
                detail = f'Choix {cell["rank"]}' if cell['rank'] else 'Hors des 3 choix'
                exact = escape(f'{cell["probability"] * 100:.10g} %', quote=True)
                content = (f'<span class="pl-scenario-probability" title="{exact}">{_percentage(cell["probability"])}</span>'
                           f'<span class="pl-scenario-meta">{detail}</span>')
            cells.append(f'<td>{content}</td>')
        if show_actual:
            cells.append('<td><span class="pl-scenario-success">Réalisé</span></td>' if row['actual'] else
                         '<td><span class="pl-scenario-status">Non réalisé</span></td>')
        realized = ' class="pl-scenario-realized"' if row['actual'] else ''
        body.append(f'<tr{realized}>{"".join(cells)}</tr>')
    return (_styles(theme) + '<div class="pl-scenario-root"><div class="pl-scenario-scroll" tabindex="0" '
            f'role="region" aria-label="{caption}"><table class="pl-scenario-table">'
            f'<caption class="pl-scenario-sr">{caption}</caption><thead><tr>{headers}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div></div>')


def render_matrix(methods, *, actual=None, key='scenario_matrix', title=None):
    """Render a frozen-method matrix and return its complete, unfiltered model."""
    try:
        matrix = build_matrix(methods, actual=actual)
    except (ValueError, TypeError, KeyError) as error:
        st.warning(f'Comparaison indisponible : {error}')
        return None
    if title:
        st.subheader(title)
    if not matrix['rows']:
        st.caption('Aucun scénario enregistré à comparer.')
        return matrix
    rows = matrix['rows']
    if len(rows) > 3:
        mode = st.segmented_control('Scénarios affichés', ['Tous les choix', 'Premiers choix'],
                                    default='Tous les choix', key=f'{key}_filter')
        if mode == 'Premiers choix':
            rows = [row for row in rows if any(cell['rank'] == 1 for cell in row['cells'].values())]
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    if pages > 1:
        fingerprint = hashlib.sha256(json.dumps(matrix, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
        page = st.selectbox('Page des scénarios', range(pages), format_func=lambda value: f'Page {value + 1} sur {pages}',
                            key=f'{key}_page_{fingerprint}')
    else:
        page = 0
    visible = rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    st.html(matrix_html(matrix, theme=_theme(), rows=visible, title=title))
    st.caption(f'{len(visible)} scénario(s) affiché(s) sur {len(rows)} · Une ligne par scénario distinct. '
               'Probabilités avant match, sans redistribution entre les seuls choix affichés.')
    st.caption('Choix 1, 2 et 3 : rang enregistré par la méthode. « Hors des 3 choix » : probabilité issue de sa '
               'distribution complète figée. Un tiret indique une probabilité non conservée dans ce classement.')
    st.caption('Cote cumulée indicative : produit des cotes fixes renseignées manuellement '
               '(1st : 3,10 · 2nd : 2,10 · Égal : 3,00) pour tous les matchs du scénario. '
               'Ces références ne sont pas des cotes relevées avant match par l’API.')
    if matrix['actual_in_choices'] is False:
        st.caption('Le scénario réalisé ne figure dans aucun des trois choix enregistrés par ces méthodes.')
    return matrix
