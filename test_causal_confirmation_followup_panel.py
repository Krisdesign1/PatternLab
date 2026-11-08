from copy import deepcopy
from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import causal_confirmation_followup_panel as panel


def sample(*, evaluated=False):
    metrics = {name: dict(count=2, logloss=.8 if name == 'candidate' else 1.,
                         brier=.5, accuracy=.5) for name in panel.METHOD_NAMES} if evaluated else {}
    counts = dict(forecasts=3 if evaluated else 0, evaluated=2 if evaluated else 0,
                  excluded=1 if evaluated else 0,
                  exclusion_reasons={'unresolved': 1} if evaluated else {})
    week = dict(week_start='2026-11-02T00:00:00+00:00', week_end='2026-11-09T00:00:00+00:00',
                anchored_days=7 if evaluated else 0, closed_days=7 if evaluated else 0,
                targets_known=4 if evaluated else 0, captures_known=3 if evaluated else 0,
                missing_captures_known=1 if evaluated else 0,
                capture_coverage=.75 if evaluated else None, counts=deepcopy(counts),
                eligible=False, reasons=['unresolved_results'] if evaluated else ['open_week', 'unknown_census'],
                metrics=deepcopy(metrics))
    return dict(registered=True, protocol_id='a'*64, source_sha256='b'*64,
                plan=dict(confirmation_start='2026-11-02T00:00:00+00:00',
                          confirmation_end='2026-12-28T00:00:00+00:00',
                          result_cutoff_utc='2027-01-04T08:00:00+00:00'),
                coverage=dict(registered_days=56, anchored_days=week['anchored_days'],
                              closed_days=week['closed_days'], unknown_census_days=49 if evaluated else 56,
                              targets_known=week['targets_known'], captures_known=week['captures_known'],
                              missing_captures_known=week['missing_captures_known'], eligible_weeks=0),
                counts=counts, weeks=[week], metrics=metrics,
                primary=dict(count=counts['evaluated'], estimate=-.2 if evaluated else None),
                inference=dict(available=False, reason='Les écarts restent descriptifs ; aucun intervalle intermédiaire n’est calculé.',
                               status='awaiting_fixed_endpoint', fixed_result_cutoff_utc='2027-01-04T08:00:00+00:00',
                               blocks=0, min_blocks=8, confidence_interval=None),
                automatic_promotion=False, predictive_improvement_demonstrated=False)


def table(at, column):
    return next(node.value for node in at.dataframe if column in node.value.columns)


class FollowupPanelTests(unittest.TestCase):
    def setUp(self):
        self.value = sample()
        self.patch = patch.object(panel, 'build', return_value=self.value)
        self.build = self.patch.start(); self.addCleanup(self.patch.stop)
        self.at = AppTest.from_string("from causal_confirmation_followup_panel import render\nrender({'id': 'loaded'})",
                                      default_timeout=10)

    def test_empty_followup_has_no_false_success_or_zero_accuracy(self):
        self.at.run(); self.assertFalse(self.at.exception)
        self.assertTrue(any('Aucun résultat admissible' in row.value for row in self.at.info))
        self.assertFalse(any('Choix corrects (%)' in row.value.columns for row in self.at.dataframe))
        self.assertFalse(self.at.success)
        self.assertEqual(table(self.at, 'Intervalle disponible').iloc[0]['Intervalle disponible'], 'Non')
        self.assertEqual(table(self.at, 'Coupure fixe des résultats (Kinshasa)').iloc[0]
                         ['Coupure fixe des résultats (Kinshasa)'], '04/01/2027 à 09:00')

    def test_matched_metrics_and_exclusions_are_visible_in_tables(self):
        self.build.return_value = sample(evaluated=True)
        self.at.run(); self.assertFalse(self.at.exception)
        metrics = table(self.at, 'Erreur logarithmique ↓')
        self.assertEqual(set(metrics['Méthode']), set(panel.METHOD_NAMES.values()))
        self.assertTrue(all(metrics['Mêmes matchs évalués'] == 2))
        self.assertEqual(table(self.at, 'Écart logarithmique observé ↓').iloc[0]['Écart logarithmique observé ↓'], -.2)
        weeks = table(self.at, 'Cibles connues sans capture')
        self.assertEqual(weeks.iloc[0]['Cibles connues sans capture'], 1)
        self.assertEqual(weeks.iloc[0]['Couverture des cibles connues (%)'], 75.)
        self.assertIn('Résultats en attente', weeks.iloc[0]['Motifs'])
        self.assertEqual(table(self.at, 'Motif').iloc[0]['Nombre'], 1)

    def test_older_protocol_is_explicit_and_does_not_invent_a_plan(self):
        self.build.return_value = dict(registered=False)
        self.at.run(); self.assertFalse(self.at.exception)
        self.assertTrue(any('ne possède pas de suivi' in row.value for row in self.at.info))
        self.assertFalse(self.at.dataframe)
        self.assertFalse(self.at.get('download_button'))

    def test_invalid_report_is_reported_without_previous_metrics(self):
        self.build.return_value = sample(evaluated=True)
        self.at.run(); self.assertTrue(self.at.dataframe)
        self.build.side_effect = ValueError('La coupure a changé')
        self.at.run(); self.assertFalse(self.at.exception)
        self.assertTrue(self.at.error)
        self.assertFalse(self.at.dataframe)

    def test_approximate_interval_never_displays_promotion_or_proven_advantage(self):
        self.value = sample(evaluated=True)
        self.value['inference'].update(available=True, blocks=8,
            reason='Intervalle approximatif sur les seuls matchs capturés et évaluables.',
            confidence_interval=dict(lower=-.3, upper=-.1, confidence=.95))
        self.build.return_value = self.value
        self.at.run(); self.assertFalse(self.at.exception)
        row = table(self.at, 'Borne basse').iloc[0]
        self.assertEqual(row['Borne basse'], -.3)
        self.assertEqual(row['Niveau nominal (%)'], 95.)
        self.assertFalse(self.at.success)
        self.assertTrue(any('sans garantie de puissance' in row.value for row in self.at.caption))

    def test_fixed_endpoint_failure_explains_why_no_interval_is_available(self):
        self.value['inference'].update(status='insufficient_complete_weeks',
            reason='Les huit semaines admissibles ne sont pas disponibles à la coupure fixe.')
        self.at.run(); self.assertFalse(self.at.exception)
        self.assertTrue(any('ne sont pas disponibles' in row.value for row in self.at.info))
        self.assertFalse(any('Borne basse' in row.value.columns for row in self.at.dataframe))

    def test_read_only_reruns_use_only_the_supplied_report_and_export_identity(self):
        import api_football as api
        import causal_candidate
        import causal_confirmation as core
        with patch.object(api.ApiFootballClient, 'get', side_effect=AssertionError('No API')), \
                patch.object(causal_candidate, 'fit_candidate', side_effect=AssertionError('No fit')), \
                patch.object(core, 'report', side_effect=AssertionError('No ledger read')), \
                patch.object(core, 'register', side_effect=AssertionError('No enrollment')):
            self.at.run(); self.at.run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.build.call_count, 2)
        self.build.assert_called_with({'id': 'loaded'})
        self.assertFalse(self.at.button)
        download = self.at.get('download_button')[0]
        self.assertEqual(download.proto.label, 'Télécharger le suivi hebdomadaire')

    def test_export_retains_protocol_identity_source_hash_and_fixed_plan(self):
        with patch.object(panel.st, 'download_button', wraps=panel.st.download_button) as download:
            self.at.run()
        self.assertFalse(self.at.exception)
        exported = json.loads(download.call_args.args[1])
        self.assertEqual(exported['protocol_id'], 'a'*64)
        self.assertEqual(exported['source_sha256'], 'b'*64)
        self.assertEqual(exported['plan']['result_cutoff_utc'], '2027-01-04T08:00:00+00:00')
        self.assertEqual(download.call_args.kwargs['file_name'], 'patternlab_suivi_hebdomadaire_aaaaaaaaaaaa.json')

    def test_actual_builder_and_empty_future_report_render_together(self):
        from causal_confirmation_followup import build, make_plan
        plan = make_plan('2026-11-02T00:00:00+00:00', '2026-12-28T00:00:00+00:00')
        first = datetime.fromisoformat(plan['confirmation_start'])
        source = dict(id='a'*64, registered_at='2026-10-31T19:00:00+00:00',
                      generated_at='2026-10-31T20:00:00+00:00', followup=plan,
                      confirmation_start=plan['confirmation_start'], confirmation_end=plan['confirmation_end'],
                      result_cutoff_utc=None, rows=[], attempts=[],
                      days=[dict(day=(first+timedelta(days=i)).date().isoformat(), state='awaiting',
                                 captures=0, targets=None) for i in range(56)])
        self.build.return_value = build(source)
        self.at.run(); self.assertFalse(self.at.exception)
        self.assertEqual(len(table(self.at, 'Début de semaine UTC')), 8)
        self.assertEqual(table(self.at, 'Intervalle disponible').iloc[0]['Intervalle disponible'], 'Non')


if __name__ == '__main__':
    unittest.main()
