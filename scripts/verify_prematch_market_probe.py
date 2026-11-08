"""Independently reconstruct a saved prematch market probe, without network/imports.

Only stdlib code is used: this deliberately imports neither the collector nor its
parser. The source envelopes, plan and summary are never modified. The report is
created exclusively, so a later run cannot silently replace the first audit.
"""
import argparse
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from hashlib import sha256
import json
from math import isclose, isfinite
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / 'data/research/prematch_market/2026-10-27-v1'
CLASSES = ('1st', 'Égal', '2nd')
LABELS = {'1st half': '1st', 'first half': '1st', 'draw': 'Égal',
          'equal': 'Égal', 'tie': 'Égal', '2nd half': '2nd', 'second half': '2nd'}
RULES_URL = 'https://help.williamhill.com/hc/en-gb/articles/28860648769565--Other-Football-Rules'


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def when(value, *, local_plan=False):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if local_plan and result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    require(result.tzinfo is not None, 'Missing explicit source timezone')
    return result.astimezone(timezone.utc)


def identity(value):
    text = str(value)
    require(not isinstance(value, bool) and text.isascii() and text.isdigit() and int(text) > 0,
            'Invalid positive provider identity')
    return str(int(text))


def close(actual, expected, name):
    require(isinstance(actual, (float, int)) and not isinstance(actual, bool)
            and isfinite(actual) and isclose(actual, float(expected), rel_tol=1e-14, abs_tol=2e-15),
            'Numerical mismatch: ' + name)


def verify(directory):
    directory = Path(directory)
    source_bytes = {}

    def load(name):
        path = directory / name
        raw = path.read_bytes()
        source_bytes[name] = raw
        return json.loads(raw)

    plan = load('plan.json')
    summary = load('summary.json')
    require(sha256((ROOT / 'prematch_market_probe.py').read_bytes()).hexdigest() == plan['code_sha256'],
            'Collector differs from pre-request frozen plan')
    plan_time = when(plan['created_at_utc'])

    def envelope(name, endpoint, params):
        result = load(name)
        require(result['transport'] == 'received' and result['http_status'] == 200,
                'Unsuccessful source envelope ' + name)
        require(result['endpoint'] == endpoint, 'Wrong requested endpoint ' + name)
        require({k: str(v) for k, v in result['parameters'].items()} == params,
                'Wrong requested parameters ' + name)
        started, observed = when(result['request_started_utc']), when(result['observed_at_utc'])
        require(plan_time <= started <= observed, 'Invalid capture chronology ' + name)
        payload = result['payload']
        require(payload['get'] == endpoint and not payload['errors'], 'Wrong/error provider payload ' + name)
        require(isinstance(payload['response'], list) and payload['results'] == len(payload['response']),
                'Response count mismatch ' + name)
        require(payload['paging'] == {'current': 1, 'total': 1}, 'Uncollected source pages ' + name)
        returned_params = payload['parameters']
        if not params:
            require(returned_params in ([], {}), 'Unexpected catalog provider parameters')
        else:
            require({k: str(v) for k, v in returned_params.items()} == params,
                    'Wrong provider parameters ' + name)
        return result, payload

    _, catalog = envelope('catalog.json', 'odds/bets', {})
    markets = [entry for entry in catalog['response'] if isinstance(entry.get('name'), str)
               and entry['name'].strip().casefold() == 'highest scoring half']
    require(len(markets) == 1, 'Aggregate exact market not unique')
    market = markets[0]
    market_id = identity(market['id'])
    require(summary['market'] == market, 'Summary market differs from catalog')
    require(summary['status'] == 'availability_probed', 'Unexpected probe status')
    require(summary['no_performance_evaluated'] is True, 'Performance boundary missing')
    require(summary['settlement_rules_verified'] is False, 'Summary settlement claim was promoted')
    require(plan['no_production_changes'] is True and plan['no_bets'] is True,
            'Plan isolation assertions missing')
    require(len(plan['fixtures']) == len(summary['fixtures']), 'Fixture count mismatch')
    require(len({identity(f['match_id']) for f in plan['fixtures']}) == len(plan['fixtures']),
            'Repeated fixture in plan')
    summary_rows = {identity(f['fixture']['match_id']): f for f in summary['fixtures']}
    require(len(summary_rows) == len(summary['fixtures']), 'Repeated summary fixture')
    rebuilt = []
    bookmaker_names = {}
    per_fixture = []

    with localcontext() as decimal_context:
        decimal_context.prec = 45
        for selected in plan['fixtures']:
            mid = identity(selected['match_id'])
            fresh_envelope, fresh = envelope('fixture_' + mid + '.json', 'fixtures', {'id': mid})
            require(len(fresh['response']) == 1, 'Fresh fixture response is not unique')
            fixture = fresh['response'][0]
            require(identity(fixture['fixture']['id']) == mid and
                    identity(fixture['league']['id']) == identity(selected['league']) and
                    fixture['league']['season'] == selected['season'], 'Fresh fixture identity differs from plan')
            require(fixture['fixture']['status']['short'] == 'NS', 'Fixture not confirmed unstarted')
            kickoff = when(fixture['fixture']['date'])
            require(kickoff == when(selected['kickoff_utc'], local_plan=True), 'Fresh kickoff differs from plan')
            require(fixture['fixture']['timestamp'] == int(kickoff.timestamp()), 'Fresh timestamp/date mismatch')
            fresh_observed = when(fresh_envelope['observed_at_utc'])
            require(fresh_observed < kickoff, 'Fresh status captured at/after kickoff')
            for side in ('home', 'away'):
                require(fixture['teams'][side]['name'] == selected[side], 'Fresh team differs from plan')
            odds_envelope, odds = envelope('odds_' + mid + '.json', 'odds',
                                           {'fixture': mid, 'bet': market_id, 'page': '1'})
            observed = when(odds_envelope['observed_at_utc'])
            require(fresh_observed <= when(odds_envelope['request_started_utc']) <= observed < kickoff,
                    'Odds capture is not after fresh check and before kickoff')
            require(len(odds['response']) == 1, 'Odds fixture response is not unique')
            offer = odds['response'][0]
            require(identity(offer['fixture']['id']) == mid and when(offer['fixture']['date']) == kickoff,
                    'Odds fixture/date differs from fresh fixture')
            require(offer['fixture']['timestamp'] == int(kickoff.timestamp()), 'Odds timestamp/date mismatch')
            require(identity(offer['league']['id']) == identity(selected['league']) and
                    offer['league']['season'] == selected['season'], 'Odds league/season mismatch')
            updated = when(offer['update'])
            require(updated <= observed < kickoff, 'Provider update is in future or capture after kickoff')
            saved = summary_rows[mid]
            require(saved['fixture'] == selected and saved['current_fixture_received'] is True
                    and saved['odds_received'] is True and saved['diagnostics'] == [],
                    'Summary fixture metadata/diagnostics differ')
            require(saved['paging'] == odds['paging'], 'Summary pagination differs from raw')
            saved_quotes = {identity(q['bookmaker_id']): q for q in saved['quotes']}
            require(len(saved_quotes) == len(saved['quotes']), 'Duplicate summary bookmaker')
            seen = set()
            fixture_quote_count = 0
            for book in offer['bookmakers']:
                bid = identity(book['id'])
                require(bid not in seen, 'Repeated raw bookmaker')
                seen.add(bid)
                exact_bets = [b for b in book['bets'] if identity(b['id']) == market_id]
                require(len(exact_bets) == 1, 'Missing/duplicate raw exact market')
                raw_market = exact_bets[0]
                require(raw_market['name'] == market['name'], 'Raw market name differs from exact catalog entry')
                values = raw_market['values']
                require(len(values) == 3, 'Raw market does not have exactly three outcomes')
                prices = {}
                for value in values:
                    raw_label = ' '.join(value['value'].casefold().split())
                    require(raw_label in LABELS, 'Unrecognized raw outcome')
                    label = LABELS[raw_label]
                    require(label not in prices, 'Duplicate raw outcome class')
                    price = Decimal(str(value['odd']))
                    require(price.is_finite() and price > 1, 'Invalid raw decimal price')
                    prices[label] = price
                require(set(prices) == set(CLASSES), 'Incomplete raw outcomes')
                inverse = [Decimal(1) / prices[c] for c in CLASSES]
                total = sum(inverse)
                probabilities = [x / total for x in inverse]
                quote = saved_quotes[bid]
                require(identity(quote['fixture_id']) == mid and identity(quote['market_id']) == market_id
                        and quote['market'] == market['name'] and quote['bookmaker'] == book['name'],
                        'Quote fixture/market/bookmaker differs from source')
                require(when(quote['kickoff_utc']) == kickoff and when(quote['provider_updated_utc']) == updated
                        and when(quote['observed_at_utc']) == observed, 'Quote timestamps differ from source')
                require(quote['settlement_rules_verified'] is False, 'Raw parser settlement flag changed')
                require(set(quote['decimal_odds']) == set(CLASSES), 'Saved quote has wrong outcomes')
                for label in CLASSES:
                    close(quote['decimal_odds'][label], prices[label], label + ' decimal price')
                require(len(quote['normalized_implied_probabilities']) == 3, 'Saved probability vector length')
                for actual, expected in zip(quote['normalized_implied_probabilities'], probabilities):
                    close(actual, expected, 'normalized implied probability')
                close(sum(quote['normalized_implied_probabilities']), 1, 'probability normalization')
                close(quote['overround'], total - 1, 'overround')
                close(quote['quote_age_hours'], (observed - updated).total_seconds() / 3600, 'quote age')
                bookmaker_names[bid] = book['name']
                rebuilt.append({
                    'fixture_id': mid, 'bookmaker_id': int(bid), 'bookmaker': book['name'],
                    'normalized_implied_probabilities': [float(p) for p in probabilities],
                    'overround': float(total - 1),
                    'quote_age_hours': (observed - updated).total_seconds() / 3600,
                    'lead_hours_before_kickoff': (kickoff - observed).total_seconds() / 3600,
                    'settlement_rules_verified_in_raw_quote': False,
                })
                fixture_quote_count += 1
            require(seen == set(saved_quotes), 'Raw/summary bookmaker sets differ')
            per_fixture.append({'fixture_id': mid, 'quotes': fixture_quote_count,
                                'fresh_status': 'NS', 'pagination_complete': True,
                                'all_quotes_captured_before_kickoff': True})

    require(summary['quote_count'] == len(rebuilt), 'Summary total quote count mismatch')
    require(summary['fixtures_with_quotes'] == sum(f['quotes'] > 0 for f in per_fixture),
            'Summary quoted-fixture count mismatch')
    source_envelopes = len(source_bytes) - 2
    require(summary['new_requests'] == source_envelopes == 1 + 2 * len(plan['fixtures']),
            'Source request count differs from summary or protocol')
    require(all((directory / name).read_bytes() == raw for name, raw in source_bytes.items()),
            'Source changed during independent verification')
    return {
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'status': 'passed',
        'method': 'Independent stdlib-only reconstruction from captured raw responses; Decimal normalization; no collector/parser import, provider call, credential access or production write.',
        'catalog_entries': len(catalog['response']), 'catalog_all_pages_present': True,
        'exact_market': market, 'source_envelopes': source_envelopes,
        'fixtures': per_fixture, 'quote_count': len(rebuilt),
        'distinct_bookmakers': [{'id': int(k), 'name': v} for k, v in sorted(bookmaker_names.items(), key=lambda p: int(p[0]))],
        'reconstructed_quotes': rebuilt,
        'source_sha256': {name: sha256(raw).hexdigest() for name, raw in sorted(source_bytes.items())},
        'verifier_sha256': sha256(Path(__file__).read_bytes()).hexdigest(),
        'all_raw_settlement_flags_preserved_false': True,
        'external_rules_review_separate_from_parser': {
            'bookmaker_id': 7, 'bookmaker': 'William Hill', 'source_url': RULES_URL,
            'source_section': 'Highest Scoring Half (not Team Highest Scoring Half)',
            'reviewed_via_public_web_on': '2026-10-27',
            'review_method': 'Human-readable primary page inspected separately; this offline verifier makes no network request and does not revalidate the live page.',
            'interpretation': 'For completed matches, compares goals across the two regulation halves including stoppage time, excludes extra time, and treats equal counts as Draw. Abandoned fixtures have separate exceptions, so this review does not equate all bookmaker settlements with completed-match labels.',
            'limited_scope': 'William Hill only; not a review of Bet365, 1xBet or Betano, and not proof of provider-to-bookmaker contractual mapping.',
            'raw_parser_flags_promoted': False,
        },
        'limits': ['Three deliberately selected upcoming fixtures, not random/global market coverage.',
                   'Normalized inverse prices are descriptive references, not calibrated true probabilities.',
                   'No outcomes or predictive accuracy were evaluated; no claim of 70% performance.'],
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = verify(args.directory)
    target = args.output or args.directory / 'independent_audit.json'
    with target.open('x') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write('\n')
    print(json.dumps({k: report[k] for k in ('status', 'catalog_entries', 'quote_count', 'source_envelopes')}, ensure_ascii=False))
