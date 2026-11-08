"""Candidate reviews stay explicit, historical reports remain accessible."""
import copy
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import model_reassessment_panel as panel


def script():
    import model_reassessment_panel
    rows = [dict(league='39', competition='Premier League') for _ in range(200)]
    model_reassessment_panel.render(rows, '39')


def table(at, column, length=None):
    return next(frame.value for frame in at.dataframe if column in frame.value.columns
                and (length is None or len(frame.value) == length))


def completed(run_id='a' * 64):
    outputs = {}
    for size in (2, 4):
        groups = 40 // size
        metrics = {name: dict(tested=groups, top1=1, top3=2, loss=float(size)) for name in panel.METHODS}
        diagnostic = dict(matches=40, logloss=1.0, brier=.6,
                          reliability={label: [dict(lower=.5, upper=.6, count=10,
                                                     mean_probability=.55, observed_rate=.5)]
                                       for label in ('1st', 'Égal', '2nd')})
        outputs[str(size)] = dict(test=metrics, selection=dict(alpha=0),
            diagnostics=dict(calibration=dict(test={name: copy.deepcopy(diagnostic)
                                                    for name in ('clubs', 'catboost_plain')})))
    fold = dict(test_season=2025, partial=False, sizes=outputs)
    return dict(run_id=run_id, source='API-Football', league='39', status='completed',
        frozen_at='2026-10-25T10:00:00+00:00', cutoff='2026-10-25', history_matches=200,
        sizes=[2, 4], candidate_generation=run_id, incumbent_generation='c' * 64,
        report=dict(candidate_generation=run_id, incumbent_generation='c' * 64, policy=dict(promotion=False),
                    native=dict(deployment=dict(trained_through='2026-10-24'), selection=dict(alpha=0),
                        test={name: dict(tested=10, top1=1, top3=2, loss=4.0)
                              for name in ('frequency', 'clubs', 'combined', 'catboost')}),
                    seasonal=dict(sizes=[2, 4], folds=[fold],
                                  aggregate={size: copy.deepcopy(value['test']) for size, value in outputs.items()})))


class ReassessmentPanelTests(unittest.TestCase):
    def setUp(self):
        panel._plan.clear()
        self.addCleanup(panel._plan.clear)
        self.items = []
        self.planned = dict(source='API-Football', league='39', history_matches=200,
            input_fingerprint='f' * 64, sizes=[2,4], ready=True, native_ready=True,
            seasonal_ready=True, reason='', new_matches=20, incumbent_generation='c' * 64,
            seasonal_folds=[dict(test_season=2025, partial=False,
                train=dict(end='2023-12-31'), validation=dict(end='2024-12-31'),
                test=dict(start='2025-01-01'))])
        self.patch(panel.review, 'plan', side_effect=lambda *args, **kwargs: copy.deepcopy(self.planned))
        self.patch(panel.review, 'list_runs', side_effect=lambda *args, **kwargs: list(self.items))
        self.loader = self.patch(panel.review, 'load_run', side_effect=lambda league, run_id:
            copy.deepcopy(next(item for item in self.items if item['run_id'] == run_id)))
        self.runner = self.patch(panel.review, 'run', side_effect=self.finish)
        self.patch(panel.admin_access, 'can_admin', return_value=True)
        self.auth = self.patch(panel.admin_access, 'require_admin')

    def patch(self, target, name, *args, **kwargs):
        patcher = patch.object(target, name, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def finish(self, *args, **kwargs):
        result = completed()
        self.items[:] = [result]
        return copy.deepcopy(result)

    def app(self):
        at = AppTest.from_function(script, default_timeout=15).run()
        self.assertFalse(at.exception)
        return at

    def test_open_filter_and_rerun_never_fit_a_candidate(self):
        at = self.app()
        at.multiselect(key='reassessment_sizes').set_value([2, 4]).run()
        at.run()
        self.assertFalse(at.exception)
        self.runner.assert_not_called()
        self.auth.assert_not_called()
        self.assertEqual(at.metric[0].value, '200')
        self.assertTrue(any('Aucune réévaluation' in item.value for item in at.info))

    def test_explicit_run_saves_candidate_and_keeps_size_scores_separate(self):
        at = self.app()
        at.button(key='reassessment_run').click().run()
        self.assertFalse(at.exception)
        self.runner.assert_called_once()
        self.auth.assert_called_once()
        self.assertEqual(table(at, 'Scénario exact en premier', 4)['Groupes testés'].tolist(), [10] * 4)
        self.assertEqual(table(at, 'Scénario exact en premier', 5)['Groupes testés'].tolist(), [10] * 5)
        at.segmented_control(key='reassessment_size_' + 'a' * 64).set_value(2).run()
        self.assertFalse(at.exception)
        self.assertEqual(table(at, 'Scénario exact en premier', 5)['Groupes testés'].tolist(), [20] * 5)
        self.assertEqual(table(at, 'Score de Brier ↓')['Matchs distincts'].tolist(), [40, 40])
        self.assertEqual(self.runner.call_count, 1)
        self.assertTrue(any('ancienne réévaluation' in item.value for item in at.info))
        self.assertTrue(any('ne remplace pas' in item.value for item in at.info))
        self.assertTrue(any('choisit ensemble les paramètres' in item.value for item in at.caption))
        self.assertTrue(any('Cette procédure diffère du protocole natif' in item.value for item in at.caption))
        self.assertTrue(any('ne valident pas directement le candidat' in item.value for item in at.caption))

    def test_past_candidates_can_be_reopened_without_training(self):
        self.items[:] = [completed('b' * 64), completed()]
        at = self.app()
        at.selectbox(key='reassessment_run_selected').set_value('a' * 64).run()
        self.assertFalse(at.exception)
        self.runner.assert_not_called()
        self.loader.assert_called_with('39', 'a' * 64)
        self.assertEqual(len(table(at, 'Identifiant')), 2)

    def test_training_failure_is_shown_and_does_not_hide_earlier_report(self):
        self.items[:] = [completed()]
        self.runner.side_effect = ValueError('Instantané non compatible')
        at = self.app()
        at.button(key='reassessment_run').click().run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Instantané non compatible' in item.value for item in at.error))
        self.assertIsNotNone(table(at, 'Scénario exact en premier'))

    def test_insufficient_history_and_read_only_users_cannot_start(self):
        self.planned.update(ready=False, reason='Saisons insuffisantes')
        at = self.app()
        self.assertTrue(at.button(key='reassessment_run').disabled)
        self.assertTrue(any('Saisons insuffisantes' in item.value for item in at.info))
        self.planned.update(ready=True)
        panel._plan.clear()
        with patch.object(panel.admin_access, 'can_admin', return_value=False):
            at.run()
        self.assertTrue(at.button(key='reassessment_run').disabled)
        self.runner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
