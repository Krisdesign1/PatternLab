"""Pure manual archive projections share browser filters, never model cohorts."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import api_football as api
import journal_browser_data as browser
import manual_analysis_store as archive
import manual_journal_rows as manual
import prediction_journal as journal


NOW = datetime(2026, 11, 6, 9, tzinfo=timezone.utc)


def entry(*, engine='statistical', probability=.3, created=None, size=2, mixed=False):
    fixtures = [dict(match_id=str(100 + i), league='61' if mixed and i % 2 else '39',
                     home_id=str(10 + i * 2), away_id=str(11 + i * 2),
                     home=f'Étoile {i}', away=f'Léopards {i}', provider='api-football',
                     timestamp=f'2026-11-06T{14 + i // 60:02}:{i % 60:02}:00') for i in range(size)]
    value = dict(source='API-Football', engine=engine, fixtures=fixtures,
        forecast=dict(eligible=True, version='saved-v1', size=size, before='2026-11-06',
                      ranking=[dict(sequence=['1st'] * size, probability=probability),
                               dict(sequence=['Égal'] * size, probability=min(.2, probability)),
                               dict(sequence=['2nd'] * size, probability=min(.1, probability))]),
        model_generation={'key': 'saved-generation'}, competition={'name': 'Première division', 'country': 'Côte d’Ivoire'})
    snapshot = archive._snapshot(value, NOW)
    return dict(snapshot, id=archive._digest(archive._semantic(snapshot)),
                created_at=(created or NOW).isoformat())


def raw(fixture, *, status='FT', ht=(1, 0), ft=(1, 0)):
    return dict(provider='api-football', fixture=dict(id=int(fixture['match_id']),
        date=fixture['timestamp'] + 'Z', status={'short': status}),
        league=dict(id=int(fixture['league']), name='Première division', country='Côte d’Ivoire', season=2026),
        teams=dict(home=dict(id=int(fixture['home_id']), name=fixture['home']),
                   away=dict(id=int(fixture['away_id']), name=fixture['away'])),
        score=dict(halftime=dict(home=ht[0], away=ht[1]), fulltime=dict(home=ft[0], away=ft[1]),
                   extratime=dict(home=8, away=7), penalty=dict(home=9, away=8)))


class ManualJournalRowsTests(unittest.TestCase):
    def test_exact_snapshot_has_namespaced_identity_and_existing_manual_origin_filter(self):
        saved = entry(engine='catboost', probability=.000000000123)
        before = deepcopy(saved)
        row = manual.build_rows([saved], [], NOW)[0]
        self.assertEqual(row['id'], 'manual:' + saved['id'])
        self.assertEqual(row['archive_id'], saved['id'])
        self.assertEqual(row['entry_id'], row['id'])
        self.assertEqual(row['record_kind'], 'manual_calculation')
        self.assertEqual(row['origin'], 'manual')
        self.assertEqual(row['origin_label'], 'Sélection manuelle')
        self.assertEqual(row['primary_method'], 'catboost')
        self.assertEqual(row['primary_probability'], .000000000123)
        self.assertEqual(row['primary_sequence'], saved['forecast']['ranking'][0]['sequence'])
        self.assertEqual(row['forecast'], saved['forecast'])
        self.assertEqual(row['fixtures'], saved['fixtures'])
        self.assertEqual((row['country'], row['competition']), ('Côte d’Ivoire', 'Première division'))
        self.assertEqual(browser.filter_rows([row], origin='manual', query='etoile'), [row])
        self.assertEqual(browser.filter_rows([row], origin='automatic'), [])
        for key in ('methods', 'evaluations', 'metadata', 'accuracy', 'loss', 'top1', 'top3'):
            self.assertNotIn(key, row)
        row['primary_sequence'][0] = 'Égal'
        row['fixtures'][0]['home'] = 'Changed'
        row['forecast']['ranking'][0]['probability'] = .9
        row['primary_generation']['key'] = 'Changed'
        self.assertEqual(saved, before)

    def test_pending_elapsed_and_partial_states_do_not_invent_verdicts(self):
        saved = entry()
        pending = manual.build_rows([saved], [], NOW)[0]
        awaiting = manual.build_rows([saved], [], NOW.replace(hour=15))[0]
        observations = [raw(saved['fixtures'][0])]
        partial = manual.build_rows([saved], observations, NOW.replace(hour=15))[0]
        self.assertEqual((pending['state'], pending['status_label']), ('pending', 'En attente'))
        self.assertEqual((awaiting['state'], awaiting['status_label']), ('awaiting_results', 'Résultats attendus'))
        self.assertEqual((partial['state'], partial['status_label']), ('in_progress', 'Résultats partiels'))
        self.assertEqual((partial['progress'], partial['total']), (1, 2))
        self.assertIn('2/3 scénarios déjà contredits',partial['result_explanation']['label'])
        self.assertIn('Comparaison initiale incomplète',partial['result_explanation']['label'])
        self.assertEqual(partial['scenario_outcome']['label'],'Évaluation complète en attente')
        self.assertEqual(browser.filter_rows([partial],verdict='miss'),[])
        for row in (pending, awaiting, partial):
            self.assertEqual(row['journal_status'], 'pending')
            self.assertEqual(row['scenario_outcome']['state'], 'pending')
            self.assertIsNone(row['scenario_outcome']['actual'])
            self.assertIsNone(row['evaluation']['top1'])

    def test_exact_saved_ranks_give_hit_and_full_miss_with_regulation_scores(self):
        saved = entry()
        hit_raw = [raw(fixture, status='PEN', ht=(0, 0), ft=(1, 0)) for fixture in saved['fixtures']]
        before = deepcopy((saved, hit_raw))
        hit = manual.build_rows([saved], hit_raw, NOW.replace(hour=20))[0]
        self.assertEqual((hit['state'], hit['status_label']), ('settled', 'Évalué'))
        self.assertEqual(hit['observed_matches'], 2)
        self.assertEqual(hit['scenario_outcome'], dict(state='hit', label='Scénario réalisé',
            matches=[dict(method='statistical', rank=3, sequence=['2nd', '2nd'])], actual=['2nd', '2nd']))
        self.assertEqual(hit['primary_probability'], .3)
        self.assertEqual(hit['primary_sequence'], ['1st', '1st'])
        self.assertFalse(hit['evaluation']['top1'])
        self.assertTrue(hit['evaluation']['top3'])
        self.assertEqual((saved, hit_raw), before)
        miss_raw = [raw(saved['fixtures'][0]), raw(saved['fixtures'][1], ht=(0, 0), ft=(1, 0))]
        miss = manual.build_rows([saved], miss_raw, NOW.replace(hour=20))[0]
        self.assertEqual(miss['scenario_outcome']['state'], 'unavailable')
        self.assertEqual(miss['scenario_outcome']['label'], 'Comparaison incomplète')
        self.assertEqual(miss['journal_status'], 'settled')
        self.assertEqual(miss['scenario_outcome']['actual'], ['1st', '2nd'])
        self.assertEqual(browser.filter_rows([hit, miss], verdict='miss'), [])
        self.assertEqual(browser.filter_rows([hit, miss], verdict='unavailable'), [miss])

    def test_cancellations_and_invalid_results_fail_closed_and_count_only_valid_matches(self):
        saved = entry()
        base = [raw(fixture) for fixture in saved['fixtures']]
        cases = []
        for code in ('CANC', 'ABD', 'AWD', 'WO'):
            observations = deepcopy(base)
            observations[1]['fixture']['status']['short'] = code
            cases.append((observations, 'void', 1))
        for code in ('NS', 'HT', '2H', 'PST', 'SUSP'):
            observations = deepcopy(base)
            observations[1]['fixture']['status']['short'] = code
            cases.append((observations, 'unresolved', 1))
        for score in ('halftime', 'fulltime'):
            observations = deepcopy(base)
            observations[1]['score'][score]['home'] = None
            cases.append((observations, 'unresolved', 1))
        observations = deepcopy(base); observations[1]['teams']['home']['id'] = 999
        cases.append((observations, 'unresolved', 1))
        observations = deepcopy(base); observations[1]['league']['id'] = 999
        cases.append((observations, 'unresolved', 1))
        cases.append((base + [deepcopy(base[1])], 'unresolved', 1))
        cases.append((base[:1], 'unresolved', 1))
        for observations, state, count in cases:
            with self.subTest(observations=observations):
                row = manual.build_rows([saved], observations, NOW.replace(hour=22))[0]
                self.assertEqual(row['state'], state)
                self.assertEqual(row['scenario_outcome']['state'], state)
                self.assertEqual(row['observed_matches'], count)
                self.assertIsNone(row['scenario_outcome']['actual'])
                self.assertEqual(row['scenario_outcome']['matches'], [])

    def test_failed_catalogue_read_has_explicit_unavailable_verdict_and_no_overdue_claim(self):
        saved = entry()
        row = manual.build_rows([saved], None, NOW.replace(hour=22))[0]
        self.assertEqual(row['state'], 'unresolved')
        self.assertEqual(row['status_label'], 'Résultats locaux indisponibles')
        self.assertEqual(row['scenario_outcome']['state'], 'unavailable')
        self.assertEqual(row['evaluation']['status'], 'unavailable')
        self.assertEqual(row['observed_matches'], 0)
        self.assertIsNone(row['evaluation']['actual'])
        self.assertTrue(all(item['matches_all'] is None for item in row['evaluation']['scenario_verdicts']))
        self.assertNotIn('délai', ' '.join(row['evaluation']['issues']))
        self.assertEqual(browser.filter_rows([row], verdict='unavailable'), [row])

    def test_recalculations_share_a_generator_without_collapsing_saved_calculations(self):
        old, new = entry(), entry(probability=.31, created=NOW.replace(minute=1))
        observations = [raw(fixture) for fixture in old['fixtures']]
        rows = manual.build_rows([old, new], iter(observations), NOW.replace(hour=20))
        self.assertEqual([row['archive_id'] for row in rows], [new['id'], old['id']])
        self.assertEqual([row['scenario_outcome']['state'] for row in rows], ['hit', 'hit'])
        mapped = manual.build_rows([old, new], {str(i): value for i, value in enumerate(observations)}, NOW.replace(hour=20))
        self.assertEqual(rows, mapped)
        counts = manual.summary(rows)
        self.assertEqual(counts, dict(total=2, pending=0, settled=2, void=0, unresolved=0,
                                     observed_matches=4, expected_matches=4))
        self.assertFalse(any('rate' in key or 'accuracy' in key or 'loss' in key for key in counts))

    def test_every_group_size_and_mixed_leagues_keep_frozen_chronological_rows(self):
        saved = [entry(size=size, mixed=size == 3) for size in range(2, 21)]
        rows = manual.build_rows(reversed(saved), [], NOW)
        self.assertEqual({row['size'] for row in rows}, set(range(2, 21)))
        mixed = next(row for row in rows if row['size'] == 3)
        self.assertEqual(mixed['league'], 'mixed')
        self.assertEqual(mixed['leagues'], ['39', '61'])
        self.assertEqual(mixed['fixtures'], saved[1]['fixtures'])
        self.assertEqual(mixed['day'], '2026-11-06')
        self.assertEqual(mixed['created_local'], '06/11/2026 à 10:00')
        self.assertEqual(mixed['kickoff_range'], '15:00–15:02')
        self.assertEqual(browser.filter_rows(rows, size=3, league='mixed'), [mixed])
        self.assertEqual([row['id'] for row in rows], sorted(row['id'] for row in rows))

    def test_tampered_archive_duplicates_and_invalid_clocks_are_rejected(self):
        saved = entry()
        changed = deepcopy(saved); changed['forecast']['ranking'][0]['probability'] = .4
        reordered = deepcopy(saved); reordered['fixtures'].reverse()
        for value in (changed, reordered, {}, 'not-an-archive'):
            with self.subTest(value=value), self.assertRaises(archive.ManualAnalysisError):
                manual.build_rows([value], [], NOW)
        with self.assertRaises(archive.ManualAnalysisError):
            manual.build_rows([saved, saved], [], NOW)
        with self.assertRaises(archive.ManualAnalysisError):
            manual.build_rows([saved], [], NOW.replace(tzinfo=None))
        with self.assertRaises(archive.ManualAnalysisError):
            manual.build_rows([saved], [], NOW.replace(hour=8))

    def test_build_and_filter_do_not_read_write_fetch_recalculate_or_make_official_entries(self):
        saved = entry()
        observations = [raw(fixture) for fixture in saved['fixtures']]
        before = deepcopy((saved, observations))
        with ExitStack() as stack:
            for target, name in ((api, 'load_key'), (api, 'load_fixtures'), (api.ApiFootballClient, 'get'),
                                 (archive, 'list_entries'), (archive, 'get'), (archive, 'record'),
                                 (journal, 'record'), (journal, 'list_entries'), (journal, '_settle')):
                stack.enter_context(patch.object(target, name, side_effect=AssertionError(name)))
            stack.enter_context(patch('builtins.open', side_effect=AssertionError('No files')))
            stack.enter_context(patch('sqlite3.connect', side_effect=AssertionError('No database')))
            rows = manual.build_rows([saved], observations, NOW.replace(hour=20))
            self.assertEqual(browser.filter_rows(rows, origin='manual', verdict='hit'), rows)
            self.assertEqual(manual.summary(rows)['settled'], 1)
        self.assertEqual((saved, observations), before)


if __name__ == '__main__':
    unittest.main()
