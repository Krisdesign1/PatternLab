"""Offline checks for the isolated market-availability probe; no provider access."""
from copy import deepcopy
import io
import json
import unittest
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import prematch_market_probe as probe


MARKET = {'id': 52, 'name': 'Highest Scoring Half'}
OBSERVED = '2026-10-27T10:00:00+00:00'
KICKOFF = '2026-10-28T18:00:00+00:00'


def catalog(*entries):
    return {'errors': [], 'response': list(entries)}


def quote_payload(labels=('1st Half', 'Draw', '2nd Half')):
    return {
        'errors': [],
        'response': [{
            'fixture': {'id': 100, 'date': KICKOFF},
            'update': '2026-10-27T09:00:00+00:00',
            'bookmakers': [{
                'id': 7, 'name': 'Synthetic bookmaker',
                'bets': [{**MARKET, 'values': [
                    {'value': label, 'odd': odd}
                    for label, odd in zip(labels, ('3.2', '3.8', '2.15'))
                ]}],
            }],
        }],
    }


def row(payload):
    return payload['response'][0]


def bet(payload):
    return row(payload)['bookmakers'][0]['bets'][0]


def parsed(payload, **kwargs):
    return probe.parse_quotes(payload, MARKET, '100', OBSERVED, **kwargs)


class MarketCatalogTests(unittest.TestCase):
    def test_exact_aggregate_market_excludes_team_variants_and_match_winner(self):
        payload = catalog(
            {'id': 1, 'name': 'Match Winner'},
            {'id': 51, 'name': 'Highest Scoring Half - Home Team'},
            MARKET,
            {'id': 53, 'name': 'Highest Scoring Half - Away Team'},
        )
        self.assertEqual(probe.find_market(payload), MARKET)
        for name in ('Highest Scoring Half - Home Team', 'Highest Scoring Half - Away Team', 'Half Time Result'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                probe.find_market(catalog({'id': 9, 'name': name}))

    def test_duplicate_exact_markets_and_invalid_catalog_are_rejected(self):
        for payload in (catalog(MARKET, MARKET), catalog(), None,
                        {'response': {}}, {'errors': {'access': 'refused'}, 'response': [MARKET]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                probe.find_market(payload)

    def test_market_id_must_be_positive_ascii_integer_not_boolean(self):
        for identity in (True, False, 0, -1, '0', '1.0', None, '１２'):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                probe.find_market(catalog({**MARKET, 'id': identity}))


class QuoteParserTests(unittest.TestCase):
    def test_valid_quote_normalizes_prices_but_does_not_assert_true_chances(self):
        payload = quote_payload()
        original = deepcopy(payload)
        result = parsed(payload, expected_kickoff=KICKOFF)
        self.assertEqual(result['diagnostics'], [])
        self.assertEqual(len(result['quotes']), 1)
        quote = result['quotes'][0]
        self.assertEqual(quote['decimal_odds'], {'1st': 3.2, 'Égal': 3.8, '2nd': 2.15})
        inverse = [1 / 3.2, 1 / 3.8, 1 / 2.15]
        for actual, expected in zip(quote['normalized_implied_probabilities'], inverse):
            self.assertAlmostEqual(actual, expected / sum(inverse))
        self.assertAlmostEqual(sum(quote['normalized_implied_probabilities']), 1)
        self.assertAlmostEqual(quote['overround'], sum(inverse) - 1)
        self.assertEqual(quote['quote_age_hours'], 1)
        self.assertIs(quote['settlement_rules_verified'], False)
        self.assertEqual(quote['fixture_id'], '100')
        self.assertEqual(payload, original)

    def test_supported_label_synonyms_preserve_class_order(self):
        for labels in (('First Half', 'Equal', 'Second Half'),
                       (' 1ST   HALF ', 'Tie', ' 2nd half ')):
            with self.subTest(labels=labels):
                self.assertEqual(parsed(quote_payload(labels))['quotes'][0]['decimal_odds'],
                                 {'1st': 3.2, 'Égal': 3.8, '2nd': 2.15})

    def test_reordered_outcomes_keep_probability_class_order(self):
        payload = quote_payload()
        bet(payload)['values'].reverse()
        self.assertEqual(parsed(payload)['quotes'][0]['normalized_implied_probabilities'],
                         parsed(quote_payload())['quotes'][0]['normalized_implied_probabilities'])

    def test_unknown_and_match_winner_labels_are_not_inferred(self):
        for label in ('1', 'X', '2', '1st', 'Home', 'Over', 'Première mi-temps'):
            payload = quote_payload()
            bet(payload)['values'][0]['value'] = label
            with self.subTest(label=label):
                self.assertEqual(parsed(payload)['quotes'], [])
                self.assertIn('unknown_outcome_label', parsed(payload)['diagnostics'])

    def test_incomplete_duplicate_and_four_way_markets_do_not_become_quotes(self):
        for mode in ('missing', 'duplicate', 'extra'):
            payload = quote_payload()
            values = bet(payload)['values']
            if mode == 'missing':
                values.pop()
            elif mode == 'duplicate':
                values[1]['value'] = 'First Half'
            else:
                values.append({'value': 'No Goal', 'odd': '8'})
            with self.subTest(mode=mode):
                self.assertEqual(parsed(payload)['quotes'], [])

    def test_decimal_odds_must_be_finite_and_greater_than_one(self):
        for odd in (None, True, False, 'nan', 'inf', '-inf', 0, -2, 1, '1', 'garbage', {}, []):
            payload = quote_payload()
            bet(payload)['values'][0]['odd'] = odd
            with self.subTest(odd=odd):
                self.assertEqual(parsed(payload)['quotes'], [])

    def test_wrong_fixture_and_boolean_provider_identity_are_rejected(self):
        for identity in (101, True, 0):
            payload = quote_payload()
            row(payload)['fixture']['id'] = identity
            with self.subTest(identity=identity):
                self.assertEqual(parsed(payload)['quotes'], [])
                self.assertIn('wrong_fixture', parsed(payload)['diagnostics'])

    def test_expected_provider_identities_must_be_valid(self):
        for identity in (True, 0, '１００', None):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                probe.parse_quotes(quote_payload(), MARKET, identity, OBSERVED)

    def test_fresh_kickoff_must_match_odds_kickoff(self):
        result = parsed(quote_payload(), expected_kickoff='2026-10-28T19:00:00+00:00')
        self.assertEqual(result['quotes'], [])
        self.assertIn('kickoff_mismatch', result['diagnostics'])

    def test_equivalent_timezones_match_the_fresh_fixture(self):
        result = parsed(quote_payload(), expected_kickoff='2026-10-28T20:00:00+02:00')
        self.assertEqual(len(result['quotes']), 1)

    def test_capture_at_or_after_kickoff_is_not_prematch(self):
        for observed in (KICKOFF, '2026-10-28T18:00:01+00:00'):
            result = probe.parse_quotes(quote_payload(), MARKET, '100', observed)
            with self.subTest(observed=observed):
                self.assertEqual(result['quotes'], [])
                self.assertIn('captured_after_kickoff', result['diagnostics'])

    def test_provider_update_cannot_be_in_the_future_of_the_capture(self):
        payload = quote_payload()
        row(payload)['update'] = '2026-10-27T10:00:01+00:00'
        result = parsed(payload)
        self.assertEqual(result['quotes'], [])
        self.assertIn('provider_update_in_future', result['diagnostics'])
        row(payload)['update'] = OBSERVED
        self.assertEqual(parsed(payload)['quotes'][0]['quote_age_hours'], 0)

    def test_naive_provider_dates_and_naive_observation_are_not_accepted(self):
        for field in ('date', 'update'):
            payload = quote_payload()
            target = row(payload)['fixture'] if field == 'date' else row(payload)
            target[field] = '2026-10-28T18:00:00'
            with self.subTest(field=field):
                self.assertEqual(parsed(payload)['quotes'], [])
        with self.assertRaises(ValueError):
            probe.parse_quotes(quote_payload(), MARKET, '100', '2026-10-27T10:00:00')

    def test_market_identity_and_name_must_both_match(self):
        for field, value in (('id', 1), ('id', True), ('name', 'Highest Scoring Half - Home Team')):
            payload = quote_payload()
            bet(payload)[field] = value
            with self.subTest(field=field, value=value):
                self.assertEqual(parsed(payload)['quotes'], [])

    def test_bad_bookmaker_or_bet_does_not_hide_other_valid_quotes(self):
        payload = quote_payload()
        row(payload)['bookmakers'].insert(0, None)
        row(payload)['bookmakers'].append({'id': 8, 'bets': [None]})
        result = parsed(payload)
        self.assertEqual(len(result['quotes']), 1)
        self.assertIn('malformed_bookmaker', result['diagnostics'])
        self.assertIn('malformed_bet', result['diagnostics'])
        for bad in (True, 0, '１２'):
            payload = quote_payload()
            row(payload)['bookmakers'][0]['id'] = bad
            with self.subTest(identity=bad):
                self.assertEqual(parsed(payload)['quotes'], [])

    def test_empty_invalid_and_error_payloads_do_not_fabricate_quotes(self):
        for payload in ({'errors': [], 'response': []}, None, {'response': {}},
                        {'errors': {'access': 'denied'}, 'response': quote_payload()['response']}):
            with self.subTest(payload=payload):
                self.assertEqual(parsed(payload)['quotes'], [])
        for books in (None, {}, 'not a list'):
            payload = quote_payload()
            row(payload)['bookmakers'] = books
            with self.subTest(books=books):
                self.assertEqual(parsed(payload)['quotes'], [])


class FakeResponse(io.BytesIO):
    status = 200

    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}


class FakeOpener:
    def __init__(self, body=b'{"errors":[],"response":[]}', *, headers=None, error=None):
        self.body = body
        self.headers = headers
        self.error = error
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return FakeResponse(self.body, self.headers)


class ReadOnlyClientTests(unittest.TestCase):
    def test_requests_use_fixed_https_host_get_and_allowlisted_parameters(self):
        opener = FakeOpener()
        client = probe.ProbeClient(key='synthetic-test-key', opener=opener)
        result = client.get('odds', fixture='100', bet=52, page=1)
        request, timeout = opener.calls[0]
        url = urlsplit(request.full_url)
        self.assertEqual((url.scheme, url.netloc, url.path),
                         ('https', 'v3.football.api-sports.io', '/odds'))
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(parse_qs(url.query), {'fixture': ['100'], 'bet': ['52'], 'page': ['1']})
        self.assertEqual(timeout, 25)
        self.assertTrue(probe.payload_ok(result))
        self.assertLessEqual(probe.stamp(result['request_started_utc']), probe.stamp(result['observed_at_utc']))

    def test_forbidden_endpoints_parameters_and_values_never_reach_opener(self):
        opener = FakeOpener()
        client = probe.ProbeClient(key='synthetic-test-key', opener=opener)
        cases = [('https://attacker.invalid/odds', {}), ('../odds', {}),
                 ('fixtures/events', {'fixture': 100}), ('fixtures', {'league': 39}),
                 ('odds/bets', {'page': 1}), ('odds', {'fixture': True}),
                 ('odds', {'fixture': '１２'}), ('odds', {'fixture': -1}),
                 ('odds', {'fixture': '100&host=evil'})]
        for endpoint, params in cases:
            with self.subTest(endpoint=endpoint, params=params), self.assertRaises(ValueError):
                client.get(endpoint, **params)
        self.assertEqual(opener.calls, [])

    def test_credential_echo_is_redacted_and_only_quota_headers_retained(self):
        key = 'synthetic-distinct-test-key'
        opener = FakeOpener(json.dumps({'errors': [], 'response': [], 'echo': key}).encode(),
                            headers={'X-Ratelimit-Requests-Remaining': '5',
                                     'Set-Cookie': 'sensitive-cookie', 'X-Secret-Echo': key})
        result = probe.ProbeClient(key=key, opener=opener).get('odds/bets')
        serialized = json.dumps(result)
        self.assertNotIn(key, serialized)
        self.assertNotIn('sensitive-cookie', serialized)
        self.assertEqual(result['payload']['echo'], '[REDACTED]')
        self.assertEqual(result['headers'], {'x-ratelimit-requests-remaining': '5'})

    def test_transport_failures_are_not_misreported_as_available_empty_market(self):
        for error, expected in ((URLError('synthetic-test-key'), 'network_error'),
                                (TimeoutError('synthetic-test-key'), 'network_error'),
                                (HTTPError('https://example.invalid', 429, 'synthetic-test-key', {}, None), 'http_error')):
            opener = FakeOpener(error=error)
            result = probe.ProbeClient(key='synthetic-test-key', opener=opener).get('odds/bets')
            with self.subTest(error=type(error).__name__):
                self.assertEqual(result['transport'], expected)
                self.assertFalse(probe.payload_ok(result))
                self.assertNotIn('synthetic-test-key', json.dumps(result))

    def test_nonfinite_json_malformed_json_and_nonobject_envelopes_are_rejected(self):
        for body, transport in ((b'{"response":[NaN]}', 'invalid_json'),
                                (b'{broken', 'invalid_json'), (b'[]', 'invalid_envelope')):
            result = probe.ProbeClient(key='synthetic-test-key', opener=FakeOpener(body)).get('odds/bets')
            with self.subTest(body=body):
                self.assertEqual(result['transport'], transport)
                self.assertFalse(probe.payload_ok(result))


if __name__ == '__main__':
    unittest.main()
