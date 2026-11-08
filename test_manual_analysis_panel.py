"""Read-only archive browsing keeps saved rankings visible without provider calls."""
from test_table_view_support import static_frames
import copy
from datetime import date, datetime, timezone
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import analysis_date
import manual_analysis_panel as panel


DAY = date(2026, 11, 6)
NOW = datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
SCRIPT = 'from manual_analysis_panel import render_manual_history\nrender_manual_history()'


def entry(identity='a', day=DAY, size=2, engine='statistical'):
    fixtures = [dict(match_id=str(100 + index), league='39', home=f'Home {index + 1}',
                     away=f'Away {index + 1}', timestamp=f'{day.isoformat()}T18:{index:02d}:00',
                     provider='api-football', home_id=str(200 + index), away_id=str(400 + index))
                for index in range(size)]
    return dict(id=identity * 64, created_at='2026-11-06T10:00:00+00:00', day=day.isoformat(),
                source='API-Football', engine=engine, fixtures=fixtures,
                competition={'name': 'Premier League'}, model_generation=None,
                forecast={'ranking': [dict(sequence=[value] * size, probability=probability)
                                      for value, probability in [('1st', .3), ('Égal', .2), ('2nd', .1)]]})


class ManualAnalysisPanelTests(unittest.TestCase):
    def setUp(self):
        self.entries = [entry()]
        self.settled = set()
        self._patch(panel, '_now', return_value=NOW)
        self.list_entries = self._patch(panel.archive, 'list_entries', side_effect=lambda day=None:
            copy.deepcopy([item for item in self.entries if day is None or item['day'] == day]))
        self.days = self._patch(panel.archive, 'days', side_effect=lambda:
            {day: sum(item['day'] == day for item in self.entries) for day in {item['day'] for item in self.entries}})
        self.get = self._patch(panel.archive, 'get', side_effect=lambda identity:
            next((copy.deepcopy(item) for item in self.entries if item['id'] == identity), None))
        self.evaluate = self._patch(panel.archive, 'evaluate', side_effect=self._evaluate)
        self.local_fixtures = self._patch(panel.api, 'load_fixtures', return_value=[])
        self.client = self._patch(panel.api, 'ApiFootballClient', side_effect=AssertionError('Unexpected provider call'))
        self.save = self._patch(panel.api, 'save_fixtures', side_effect=AssertionError('Unexpected shared-data write'))
        self.record = self._patch(panel.archive, 'record', side_effect=AssertionError('Browsing must not save a calculation'))

    def _patch(self, target, name, **kwargs):
        patcher = patch.object(target, name, **kwargs)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def _evaluate(self, item, raw, now=None):
        settled = item['id'] in self.settled
        return dict(id=item['id'], status='settled' if settled else 'pending',
                    status_label='Évalué' if settled else 'En attente',
                    actual=['1st'] * len(item['fixtures']) if settled else None,
                    top1=True if settled else None, top3=True if settled else None, issues=[],
                    scenario_verdicts=[dict(rank=index, sequence=row['sequence'], probability=row['probability'],
                                           matches_all=(index == 1) if settled else None)
                                       for index, row in enumerate(item['forecast']['ranking'], 1)])

    def _app(self, selected_day=DAY, query=None):
        at = AppTest.from_string(SCRIPT, default_timeout=10)
        at.session_state[analysis_date.DAY_KEY] = selected_day
        at.session_state['analysis_follow_today'] = False
        if query is not None:
            at.query_params['manual_analysis'] = query
        at.run()
        self.assertFalse(at.exception)
        return at

    def _overview(self, at):
        return next(item.value for item in at.dataframe if 'Scénario 1' in item.value.columns)

    def _no_mutations(self):
        self.client.assert_not_called()
        self.save.assert_not_called()
        self.record.assert_not_called()

    def test_saved_scenarios_and_all_twenty_fixtures_are_inline_and_read_only(self):
        self.entries = [entry(size=20, engine='catboost')]
        original = copy.deepcopy(self.entries)
        at = self._app()
        overview = self._overview(at)
        self.assertEqual(overview.iloc[0]['Matchs'], 20)
        self.assertEqual(overview.iloc[0]['Moteur'], 'CatBoost entraîné')
        self.assertEqual(overview.iloc[0]['Probabilité 1 (%)'], 30)
        fixtures = next(item.value for item in at.dataframe if 'Domicile' in item.value.columns)
        self.assertEqual(len(fixtures), 20)
        self.assertEqual(fixtures.iloc[-1]['Domicile'], 'Home 20')
        scenarios = static_frames(at)[-1]
        self.assertEqual(len(scenarios), 3)
        self.assertEqual(list(scenarios['Probabilité enregistrée (moteur choisi)']), ['30,00 %', '20,00 %', '10,00 %'])
        self.assertEqual(list(scenarios['Suivi du scénario']), ['En attente'] * 3)
        self.assertEqual(scenarios.iloc[0]['Scénario complet'].count('1st'), 20)
        self.assertEqual(self.entries, original)
        self.local_fixtures.assert_called_once_with(ids=[str(value) for value in range(100, 120)])
        self.assertTrue(any('Historique partagé du site' in item.value for item in at.caption))
        self.assertFalse(at.expander)
        at.run()
        self.assertFalse(at.exception)
        self._no_mutations()

    def test_selected_tomorrow_day_excludes_today_and_history_lists_past_only(self):
        tomorrow = date(2026, 11, 7)
        yesterday = date(2026, 11, 5)
        self.entries = [entry('a'), entry('b', yesterday), entry('c', tomorrow)]
        at = self._app(tomorrow)
        self.assertEqual(len(self._overview(at)), 1)
        self.assertEqual(at.selectbox(key='manual_archive_selected_id').value, 'c' * 64)
        at.segmented_control(key='manual_archive_view').set_value('history').run()
        self.assertFalse(at.exception)
        history = at.selectbox(key='manual_archive_history_day')
        self.assertEqual(history.value, yesterday.isoformat())
        self.assertEqual(len(history.options), 1)
        self.assertEqual(at.selectbox(key='manual_archive_selected_id').value, 'b' * 64)
        self._no_mutations()

    def test_size_filter_keeps_whole_group_and_all_three_scenarios(self):
        self.entries = [entry('a', size=2), entry('b', size=3)]
        at = self._app()
        at.selectbox(key='manual_archive_size').set_value(3).run()
        self.assertFalse(at.exception)
        self.assertEqual(list(self._overview(at)['Matchs']), [3])
        self.assertEqual(len(static_frames(at)[-1]), 3)
        self.assertEqual(at.selectbox(key='manual_archive_selected_id').value, 'b' * 64)
        self._no_mutations()

    def test_link_opens_past_record_without_changing_global_date(self):
        self.entries = [entry('b', date(2026, 11, 5))]
        self.settled.add('b' * 64)
        at = self._app(query='b' * 64)
        self.get.assert_called_once_with('b' * 64)
        self.assertEqual(at.session_state[analysis_date.DAY_KEY], DAY)
        self.assertEqual(len(static_frames(at)), 1)
        self.assertEqual(list(static_frames(at)[-1]['Suivi du scénario']),
                         ['Réalisé', 'Non réalisé', 'Non réalisé'])
        summary=next(item.value for item in at.dataframe if 'Exact dans les 3 (moteur choisi)' in item.value.columns)
        self.assertEqual(summary.iloc[0]['Exact dans les 3 (moteur choisi)'], 'Oui')
        self.assertTrue(any('Calcul ouvert depuis le lien' in item.value for item in at.caption))
        self._no_mutations()

    def test_invalid_link_never_becomes_a_store_lookup_or_path(self):
        at = self._app(query='../../secrets.toml')
        self.get.assert_not_called()
        self.assertTrue(any('lien' in item.value for item in at.warning))
        self._no_mutations()

    def test_empty_history_does_not_request_local_scores(self):
        self.entries = []
        at = self._app()
        self.assertTrue(any('Aucun calcul manuel' in item.value for item in at.info))
        self.local_fixtures.assert_not_called()
        self._no_mutations()

    def test_storage_failure_is_visible_without_forecast_or_save(self):
        self.list_entries.side_effect = panel.archive.ManualAnalysisError('lecture impossible')
        at = self._app()
        self.assertTrue(any('lecture impossible' in item.value for item in at.error))
        self.local_fixtures.assert_not_called()
        self._no_mutations()

    def test_score_read_failure_preserves_saved_scenarios_without_success_claim(self):
        self.local_fixtures.side_effect = panel.api.ApiFootballError('catalogue indisponible')
        at = self._app()
        self.assertTrue(any('catalogue indisponible' in item.value for item in at.warning))
        self.assertEqual(self._overview(at).iloc[0]['État des résultats locaux'], 'Résultats locaux indisponibles')
        self.assertEqual(len(static_frames(at)[-1]), 3)
        self.assertEqual(list(static_frames(at)[-1]['Suivi du scénario']), ['Vérification indisponible'] * 3)
        self.evaluate.assert_not_called()
        self._no_mutations()

    def test_partial_five_match_capture_shows_contradictions_and_missing_match_in_tables(self):
        saved=entry(size=5)
        choices=[['2nd']*5,['2nd','1st','2nd','2nd','2nd'],['2nd','2nd','1st','2nd','2nd']]
        for row,sequence,probability in zip(saved['forecast']['ranking'],choices,[.023544,.022243,.021498]):
            row.update(sequence=sequence,probability=probability)
        self.entries=[saved]
        outcomes=['1st','2nd','1st','2nd',None]
        result=dict(id=saved['id'],status='pending',status_label='En attente des résultats',actual=None,
                    top1=None,top3=None,issues=[],matches=[dict(fixture,status='settled' if value else 'pending',
                    actual=value,reason='Confirmé.' if value else 'Résultat API-Football non confirmé.',
                    **({'ht':'1-0','ft':'1-0'} if value else {}))
                    for fixture,value in zip(saved['fixtures'],outcomes)])
        original=copy.deepcopy((saved,result))
        self.evaluate.side_effect=lambda *args,**kwargs:copy.deepcopy(result)
        at=self._app()
        summary=next(item.value for item in at.dataframe if 'État du groupe' in item.value.columns)
        self.assertEqual(summary.iloc[0]['État du groupe'],'Groupe incomplet')
        self.assertEqual(summary.iloc[0]['Résultats confirmés'],'4/5')
        self.assertEqual(summary.iloc[0]['Exact dans les 3 (moteur choisi)'],'Non évalué')
        matches=next(item.value for item in at.dataframe if 'Domicile' in item.value.columns)
        self.assertEqual(matches.iloc[-1]['Mi-temps'],'Non reçu')
        self.assertEqual(matches.iloc[-1]['Statut du match'],'En attente')
        self.assertIn('non confirmé',matches.iloc[-1]['Motif'])
        scenarios=static_frames(at)[-1]
        self.assertEqual(list(scenarios['Suivi du scénario']),['Déjà contredit']*3)
        self.assertEqual(scenarios.iloc[0]['Probabilité enregistrée (moteur choisi)'],'2,35 %')
        self.assertTrue(all('Match 1 : choix 2nd, résultat 1st' in text for text in scenarios['Motif']))
        self.assertEqual((saved,result),original)
        self._no_mutations()


if __name__ == '__main__':
    unittest.main()
