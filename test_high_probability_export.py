"""Exports preserve saved forecasts, category semantics and spreadsheet safety."""
from copy import deepcopy
import csv
from datetime import date, datetime, timezone
from io import StringIO
import json
import math
import unittest
from unittest.mock import patch

import high_probability_export as export


NOW = datetime(2026, 10, 28, 9, tzinfo=timezone.utc)


def saved(identifier='100', *, day='2026-10-27', status='settled', actual='1st'):
    return dict(id=f'archive-{identifier}', source='API-Football', source_kind='ranking_archive',
        match_id=identifier, day=day,
        fixture=dict(match_id=identifier, league='39', provider='api-football', home_id='11', away_id='12',
            home='Home', away='Away', country='England', competition='Premier League',
            timestamp=f'{day}T14:00:00+00:00'),
        competition='England · Premier League', created_at=f'{day}T09:00:00+00:00', before=day,
        probabilities={'1st': .6, 'Égal': .1, '2nd': .3},
        baseline={'1st': .3, 'Égal': .3, '2nd': .4}, status=status,
        actual=actual if status == 'settled' else None,
        ht='1-0' if status == 'settled' else None, ft='1-0' if status == 'settled' else None,
        model_version='clubs-saved-test', training=123, history_days=20,
        result_explanation={})


def build(rows, *, days=None, outcomes='all', minimum=0):
    return export.build(rows, days=days, outcomes=outcomes, minimum=minimum, now=NOW)


class HighProbabilityExportTests(unittest.TestCase):
    def test_multiple_days_and_categories_have_one_row_per_match_and_category(self):
        inputs = [saved(), saved('101'), saved('200', day='2026-10-26')]
        original = deepcopy(inputs)
        rows = build(inputs, days=[date(2026, 10, 26), '2026-10-27'], outcomes=['2nd', '1st', 'Égal'])
        self.assertEqual(len(rows), 9)
        self.assertEqual(len({(row['Journée'], row['ID du match'], row['Catégorie']) for row in rows}), 9)
        self.assertEqual([row['Catégorie'] for row in rows[:3]], list(export.VALUES))
        self.assertEqual(inputs, original)
        row = rows[0]
        self.assertEqual(list(row), list(export.COLUMNS))
        self.assertEqual(row['Match'], 'Home — Away')
        self.assertEqual((row['Club domicile'], row['Club extérieur'], row['Pays']), ('Home', 'Away', 'England'))
        self.assertEqual(row['Coup d’envoi (Kinshasa)'], '2026-10-27T15:00:00+01:00')
        self.assertEqual(row['Capture UTC'], '2026-10-27T09:00:00+00:00')
        self.assertEqual(row['Référence archivée'], 'archive-100')
        self.assertEqual(row['Version du modèle'], 'clubs-saved-test')
        self.assertEqual(row['Provenance'], 'Classement archivé')
        self.assertEqual(row['Source prévision'], 'API-Football')
        self.assertEqual(row['Coupure historique UTC'], '2026-10-27')
        self.assertEqual(row['Résultats historiques'], 123)
        self.assertEqual([row[f'{value} (%)'] for value in export.VALUES], [60, 10, 30])

    def test_dates_and_categories_filter_independently_without_reconstructing_days(self):
        inputs = [saved(), saved('200', day='2026-10-26')]
        rows = build(inputs, days='2026-10-26', outcomes='Égal')
        self.assertEqual([(row['Journée'], row['ID du match'], row['Catégorie']) for row in rows],
                         [('2026-10-26', '200', 'Égal')])
        self.assertEqual(build(inputs, days=['2026-10-25']), [])
        self.assertEqual(build(inputs, days=[]), [])
        self.assertEqual(build(inputs, outcomes=[]), [])
        self.assertEqual(len(build(inputs, outcomes='Toutes')), 6)

    def test_local_day_comes_from_kinshasa_kickoff(self):
        row = saved(status='pending')
        row['fixture']['timestamp'] = '2026-10-26T23:30:00Z'
        row['created_at'] = '2026-10-26T09:00:00Z'
        rows = build([row], days='2026-10-27', outcomes='1st')
        self.assertEqual(rows[0]['Coup d’envoi (Kinshasa)'], '2026-10-27T00:30:00+01:00')
        row['day'] = '2026-10-26'
        with self.assertRaisesRegex(ValueError, 'journée'):
            build([row])

    def test_each_category_uses_its_own_exact_threshold_before_rounding(self):
        row = saved()
        self.assertEqual([item['Catégorie'] for item in build([row], minimum=.3)], ['1st', '2nd'])
        self.assertEqual([item['Catégorie'] for item in build([row], minimum=.6)], ['1st'])
        self.assertEqual(build([row], minimum=.6000000001), [])
        row['probabilities'] = {'1st': .599999999, 'Égal': .100000001, '2nd': .3}
        self.assertEqual(build([row], outcomes='1st', minimum=.6), [])
        row['probabilities'] = {'1st': 1, 'Égal': 0, '2nd': 0}
        self.assertEqual(len(build([row])), 3)

    def test_duplicate_matches_use_first_projected_reference(self):
        first = saved()
        later = deepcopy(first)
        later.update(id='a-different-reference', created_at='2026-10-27T10:00:00Z')
        rows = build([first, later, first])
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row['Référence archivée'] == first['id'] for row in rows))
        self.assertEqual(len(build([first, saved(day='2026-10-26')])), 6)

    def test_conflicting_duplicate_identity_probabilities_or_result_is_refused(self):
        first = saved()
        for field, value in [('home', 'Different club'), ('home_id', '999'),
                             ('timestamp', '2026-10-27T15:00:00Z')]:
            with self.subTest(field=field):
                second = deepcopy(first)
                second['fixture'][field] = value
                with self.assertRaisesRegex(ValueError, 'contradictoires'):
                    build([first, second], minimum=1)
        second = deepcopy(first)
        second['probabilities'] = {'1st': .2, 'Égal': .3, '2nd': .5}
        with self.assertRaisesRegex(ValueError, 'contradictoires'):
            build([first, second], outcomes='1st', minimum=.5)
        second = deepcopy(first)
        second['ft'] = '2-0'
        with self.assertRaisesRegex(ValueError, 'contradictoires'):
            build([first, second])

    def test_fixed_manual_reference_odds_and_saved_baseline_are_per_category(self):
        rows = build([saved()])
        self.assertEqual([row['Cote de référence'] for row in rows], [3.10, 3.00, 2.10])
        self.assertTrue(all('fixes manuelles' in row['Source de la cote'] for row in rows))
        self.assertTrue(all('aucune cote de marché API' in row['Source de la cote'] for row in rows))
        self.assertEqual([row['Tendance (%)'] for row in rows], [30, 30, 40])
        for row, expected in zip(rows, (30, -20, -10)):
            self.assertAlmostEqual(row['Écart (points)'], expected)
        for baseline in (None, {}, {'1st': .3, 'Égal': .3, '2nd': .3},
                         {'1st': math.nan, 'Égal': .3, '2nd': .7}):
            source = saved()
            source['baseline'] = baseline
            self.assertTrue(all(row['Tendance (%)'] is None and row['Écart (points)'] is None
                                for row in build([source])))
        source['baseline'] = {'1st': 0, 'Égal': .6, '2nd': .4}
        self.assertEqual(build([source], outcomes='1st')[0]['Tendance (%)'], 0)

    def test_scores_real_outcome_and_category_verdicts_are_saved_values(self):
        rows = build([saved()])
        self.assertEqual([row['Verdict'] for row in rows], ['Réalisé', 'Non réalisé', 'Non réalisé'])
        self.assertTrue(all(row['Score à la pause'] == '1-0' and row['Score à 90 minutes'] == '1-0'
                            and row['Résultat réel'] == '1st' for row in rows))
        source = saved(status='pending')
        row = build([source], outcomes='2nd')[0]
        self.assertEqual(row['Verdict'], 'Non évalué')
        self.assertIsNone(row['Score à la pause'])
        self.assertIsNone(row['Score à 90 minutes'])
        self.assertIsNone(row['Résultat réel'])
        source['status'] = 'void'
        self.assertEqual(build([source])[0]['Verdict'], 'Annulé du suivi')

    def test_future_fixture_cannot_receive_scores_or_final_verdict_even_in_bad_input(self):
        for state in ('pending', 'settled'):
            source = saved(status=state)
            source['day'] = '2026-10-29'
            source['fixture']['timestamp'] = '2026-10-29T14:00:00Z'
            original = deepcopy(source)
            rows = build([source])
            self.assertTrue(all(row['Verdict'] == 'En attente' and row['État'] == 'pending' for row in rows))
            self.assertTrue(all(row['Score à la pause'] is None and row['Score à 90 minutes'] is None
                                and row['Résultat réel'] is None for row in rows))
            self.assertEqual(source, original)

    def test_annotation_is_preserved_and_no_io_or_clock_is_needed(self):
        source = saved(status='unresolved')
        source['result_explanation'] = dict(label='À vérifier', summary='Résumé', matches=[
            dict(match_id='100', label='Horaire à vérifier', reason='Décalage archivé')])
        with patch('builtins.open', side_effect=AssertionError('Pure export must not read or write files')):
            rows = build([source], outcomes='Égal')
            export.to_csv(rows)
            export.to_json(rows)
        self.assertEqual(rows[0]['Statut'], 'Horaire à vérifier')
        self.assertEqual(rows[0]['Motif'], 'Décalage archivé')

    def test_invalid_clock_threshold_forecast_and_categories_fail_clearly(self):
        for minimum in (-.1, 1.1, math.nan, True, '0.5'):
            with self.subTest(minimum=minimum), self.assertRaises(ValueError):
                build([saved()], minimum=minimum)
        with self.assertRaises(ValueError):
            export.build([saved()], days=None, outcomes='all', now=NOW.replace(tzinfo=None))
        with self.assertRaises(ValueError):
            build([saved()], outcomes='unknown')
        source = saved()
        source['created_at'] = source['fixture']['timestamp']
        with self.assertRaisesRegex(ValueError, 'capture'):
            build([source])
        source = saved()
        source['probabilities']['1st'] = math.nan
        with self.assertRaisesRegex(ValueError, 'probabilités'):
            build([source])


class HighProbabilitySerializationTests(unittest.TestCase):
    def test_csv_bom_semicolons_french_numbers_quoting_and_missing_values(self):
        source = saved(status='pending')
        source['fixture']['home'] = 'Club; "Étoile"\nDeuxième ligne'
        source.pop('baseline')
        encoded = export.to_csv(build([source], outcomes='2nd'))
        self.assertTrue(encoded.startswith(b'\xef\xbb\xbf'))
        parsed = list(csv.DictReader(StringIO(encoded.decode('utf-8-sig')), delimiter=';'))
        self.assertEqual(len(parsed), 1)
        row = parsed[0]
        self.assertEqual(row['Club domicile'], source['fixture']['home'])
        self.assertEqual(row['Probabilité (%)'], '30,00')
        self.assertEqual(row['Cote de référence'], '2,10')
        self.assertEqual(row['Tendance (%)'], '')
        self.assertEqual(row['Écart (points)'], '')
        self.assertEqual(row['Score à la pause'], '')
        self.assertEqual(row['Score à 90 minutes'], '')
        self.assertEqual(row['Résultat réel'], '')
        row = list(csv.DictReader(StringIO(export.to_csv(build([saved()], outcomes='Égal')).decode('utf-8-sig')),
                                 delimiter=';'))[0]
        self.assertEqual(row['Écart (points)'], '-20,00')

    def test_all_external_formula_prefixes_are_escaped_without_altering_json(self):
        for prefix in ('=', '+', '-', '@', '\t', '\r', '\n', '  =', '\ufeff@'):
            with self.subTest(prefix=repr(prefix)):
                source = saved()
                source['fixture']['home'] = prefix + 'EXTERNAL()'
                rows = build([source], outcomes='1st')
                original = deepcopy(rows)
                csv_row = list(csv.DictReader(StringIO(export.to_csv(rows).decode('utf-8-sig')), delimiter=';'))[0]
                self.assertEqual(csv_row['Club domicile'], "'" + source['fixture']['home'])
                self.assertEqual(json.loads(export.to_json(rows))['rows'][0]['Club domicile'],
                                 source['fixture']['home'])
                self.assertEqual(rows, original)

    def test_zero_baseline_stays_zero_and_integer_count_remains_a_number(self):
        source = saved()
        source['baseline'] = {'1st': 0, 'Égal': .6, '2nd': .4}
        rows = build([source], outcomes='1st')
        parsed = list(csv.DictReader(StringIO(export.to_csv(rows).decode('utf-8-sig')), delimiter=';'))[0]
        self.assertEqual(parsed['Tendance (%)'], '0,00')
        self.assertEqual(parsed['Résultats historiques'], '123')

    def test_json_preserves_numbers_missingness_and_explicit_selection_metadata(self):
        source = saved(status='pending')
        source.pop('baseline')
        rows = build([source], outcomes='Égal')
        encoded = export.to_json(rows, days=['2026-10-26', '2026-10-27'], outcomes='all')
        self.assertIsInstance(encoded, bytes)
        self.assertFalse(encoded.startswith(b'\xef\xbb\xbf'))
        payload = json.loads(encoded)
        self.assertEqual(payload['selection'], dict(days=['2026-10-26', '2026-10-27'], outcomes=list(export.VALUES)))
        self.assertEqual(payload['timezone'], 'Africa/Kinshasa')
        self.assertEqual(payload['odds_reference']['kind'], 'manual_fixed')
        self.assertFalse(payload['odds_reference']['market_api'])
        self.assertEqual(payload['odds_reference']['values'], {'1st': 3.10, 'Égal': 3.00, '2nd': 2.10})
        self.assertEqual(payload['rows'], rows)
        self.assertEqual(payload['rows'][0]['Probabilité (%)'], 10)
        self.assertIsNone(payload['rows'][0]['Tendance (%)'])
        inferred = json.loads(export.to_json(rows))['selection']
        self.assertEqual(inferred, dict(days=['2026-10-27'], outcomes=['Égal']))

    def test_empty_exports_keep_csv_headers_and_requested_json_selection(self):
        self.assertEqual(list(csv.DictReader(StringIO(export.to_csv([]).decode('utf-8-sig')), delimiter=';')), [])
        self.assertIn('Cote de référence', export.to_csv([]).decode('utf-8-sig'))
        payload = json.loads(export.to_json([], days='2026-10-27', outcomes='2nd'))
        self.assertEqual(payload['selection'], dict(days=['2026-10-27'], outcomes=['2nd']))
        self.assertEqual(payload['rows'], [])


if __name__ == '__main__':
    unittest.main()
