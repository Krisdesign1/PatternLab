"""Evidence rendering is read-only and leaves the original journal cohort alone."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import api_football as api
import catboost_scenarios as ml
import journal_evidence_panel as panel
import journal_panel as journal_ui
import journal_protocol as protocol_store


class JournalEvidencePanelTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.root=Path(directory.name)
        self.protocol=dict(version='journal-protocol-v1',registered_at='2026-10-19T10:00:00Z',
                           baseline='clubs',confidence=.95,bootstrap_samples=4000,min_blocks=20)
        self.entries=[dict(id='frozen-entry',revision=1,league='39',size=4)]
        self.result=dict(protocol_version='journal-protocol-v1',legacy_count=3,prospective_count=42,
            comparisons=[dict(method='combined',baseline='clubs',groups=40,blocks=22,status='favorable',reason='',
                delta_loss=dict(estimate=.05,lower=.01,upper=.09),
                delta_top3=dict(estimate=.03,lower=-.01,upper=.07),
                excluded=dict(pending=2,missing_methods=1,invalid=1,open_week=4,incomplete_block=3))])
        self.loader=self.patch(panel.protocol_store,'load_protocol',return_value=self.protocol)
        self.validator=self.patch(panel.protocol_store,'validate_protocol',side_effect=lambda value:value)
        self.evaluator=self.patch(panel.uncertainty,'evaluate',return_value=self.result)
        self.network=self.patch(api.ApiFootballClient,'get',side_effect=AssertionError('No network in evidence view'))
        self.patch(api,'DATA_DIR',self.root)
        panel._evaluate.clear();self.addCleanup(panel._evaluate.clear)

    def patch(self,target,name,*args,**kwargs):
        patcher=patch.object(target,name,*args,**kwargs)
        result=patcher.start();self.addCleanup(patcher.stop);return result

    def open(self,methods=None,protocol=None):
        source=f"import journal_evidence_panel as panel\nentries={self.entries!r}\nmethods={methods or ['clubs','combined']!r}\n"
        source += 'panel.render(entries,methods)' if protocol is None else f'panel.render(entries,methods,protocol={protocol!r})'
        at=AppTest.from_string(source,default_timeout=15).run()
        self.assertFalse(at.exception)
        return at

    def test_no_protocol_is_descriptive_and_never_evaluates_or_writes(self):
        self.loader.return_value=None
        at=self.open()
        self.assertTrue(any('Aucun protocole prospectif' in item.value for item in at.info))
        self.evaluator.assert_not_called();self.validator.assert_not_called();self.network.assert_not_called()
        self.assertEqual(list(self.root.iterdir()),[])
        self.assertFalse(at.dataframe)

    def test_baseline_must_be_selected_without_changing_the_existing_selection(self):
        methods=['combined','catboost']
        at=self.open(methods)
        self.assertTrue(any('Ajoutez « Clubs seuls »' in item.value for item in at.info))
        self.assertFalse(at.multiselect)
        self.assertEqual(methods,['combined','catboost'])
        self.evaluator.assert_not_called();self.network.assert_not_called()

    def test_intervals_and_percentage_point_units_preserve_signed_observations(self):
        before=copy.deepcopy(self.entries)
        at=self.open(protocol=self.protocol)
        self.loader.assert_not_called()
        self.validator.assert_called_once_with(self.protocol)
        self.assertTrue(any('19/10/2026 à 10:00:00 UTC' in item.value for item in at.caption))
        table=at.dataframe[0].value
        self.assertEqual(table.iloc[0]['État'],'Écart favorable observé')
        self.assertEqual(table.iloc[0]['Réduction d’erreur probabiliste'],'+0.0500')
        self.assertEqual(table.iloc[0]['Écart dans les 3 (points de %)'],'+3.0000')
        self.assertEqual(table.iloc[0]['Intervalle à 95 % · erreur'],'[+0.0100 ; +0.0900]')
        self.assertEqual(table.iloc[0]['Intervalle à 95 % · dans les 3 (points)'],'[-1.0000 ; +7.0000]')
        self.assertTrue(any('3 ancienne(s)' in item.value and '42 prévision(s)' in item.value for item in at.caption))
        self.assertTrue(any('2 en attente' in item.value and '4 dans des semaines encore ouvertes' in item.value for item in at.caption))
        self.assertTrue(any('3 dans des blocs encore incomplets' in item.value for item in at.caption))
        self.assertTrue(any('fermeture complète' in item.value for item in at.caption))
        self.assertTrue(any('consultations répétées' in item.value for item in at.caption))
        self.assertTrue(any('ne confirme pas un gain prédictif' in item.value for item in at.caption))
        self.assertEqual(self.entries,before)
        self.assertEqual(self.evaluator.call_args.args[:2],(self.entries,['clubs','combined']))
        self.assertIsNotNone(self.evaluator.call_args.kwargs['now'].tzinfo)
        self.network.assert_not_called()
        self.assertEqual(list(self.root.iterdir()),[])

    def test_insufficient_support_never_invents_interval_and_other_states_are_explicit(self):
        self.result['comparisons'][0].update(status='insufficient',blocks=3,reason='Au moins 20 blocs sont requis.',
            delta_loss=dict(estimate=.05,lower=None,upper=None),delta_top3=dict(estimate=0,lower=None,upper=None))
        at=self.open()
        table=at.dataframe[0].value
        self.assertEqual(table.iloc[0]['État'],'Données insuffisantes')
        self.assertEqual(table.iloc[0]['Intervalle à 95 % · erreur'],'Non disponible')
        self.assertTrue(any('Au moins 20 blocs' in item.value for item in at.caption))
        for status,label in [('inconclusive','Écart incertain'),('unfavorable','Écart défavorable observé')]:
            self.result['comparisons'][0]['status']=status
            panel._evaluate.clear()
            at=self.open()
            self.assertEqual(at.dataframe[0].value.iloc[0]['État'],label)

    def test_reruns_use_bounded_cache_but_result_revisions_invalidate_it(self):
        at=self.open()
        at.run()
        self.assertEqual(self.evaluator.call_count,1)
        self.entries[0]['revision']=2
        self.open()
        self.assertEqual(self.evaluator.call_count,2)
        self.network.assert_not_called()

    def test_invalid_protocol_and_evaluation_errors_do_not_crash_the_journal_view(self):
        self.validator.side_effect=ValueError('Protocole incompatible')
        at=self.open()
        self.assertTrue(any('Protocole incompatible' in item.value for item in at.warning))
        self.evaluator.assert_not_called()
        self.validator.side_effect=None
        self.validator.return_value=self.protocol
        self.evaluator.side_effect=ValueError('Cohorte incohérente')
        at=self.open()
        self.assertTrue(any('Cohorte incohérente' in item.value for item in at.warning))

    def test_journal_integration_passes_only_the_existing_cohort_without_changing_old_results(self):
        entries=[dict(id='old',methods={'clubs':{},'combined':{}})]
        result=dict(entries=self.entries, counts=dict(total=1,pending=0,unresolved=0),settled=[],
                    missing_methods=0,other_versions=0,summary=[],trend=[])
        with patch.object(journal_ui.performance,'signatures',return_value=[]), \
             patch.object(journal_ui.performance,'comparison',return_value=result) as compare, \
             patch.object(journal_ui.evidence,'render') as rendered:
            source=f"import journal_panel\njournal_panel._comparison({entries!r}, '39', 4, 'all')"
            at=AppTest.from_string(source,default_timeout=15).run()
        self.assertFalse(at.exception)
        compare.assert_called_once_with(entries,['clubs','combined'],None)
        rendered.assert_called_once_with(self.entries,['clubs','combined'])
        self.assertEqual(entries,[dict(id='old',methods={'clubs':{},'combined':{}})])


class RealEvidencePanelTests(unittest.TestCase):
    def test_real_protocol_and_computation_preserve_inputs_without_provider_or_fit(self):
        from test_journal_uncertainty import entry, protocol
        rows=[entry(0),entry(1)]
        rows[0]['created_at']='2025-12-31T23:59:59+00:00'
        before=copy.deepcopy(rows)
        source=f'import journal_evidence_panel as panel\npanel.render({rows!r}, ["clubs","combined"], protocol={protocol()!r})'
        panel._evaluate.clear()
        try:
            with patch.object(protocol_store,'load_protocol',side_effect=AssertionError('Explicit protocol is already supplied')), \
                 patch.object(api.ApiFootballClient,'get',side_effect=AssertionError('No provider request')) as network, \
                 patch.object(ml,'train',side_effect=AssertionError('No fit')) as trained:
                at=AppTest.from_string(source,default_timeout=15).run()
            self.assertFalse(at.exception)
            self.assertFalse(at.warning)
            self.assertTrue(any('1 ancienne(s)' in item.value and '1 prévision(s)' in item.value for item in at.caption))
            table=at.dataframe[0].value
            self.assertEqual(table.iloc[0]['Groupes comparés'],1)
            self.assertEqual(table.iloc[0]['Blocs de dépendance'],1)
            self.assertEqual(table.iloc[0]['État'],'Données insuffisantes')
            network.assert_not_called();trained.assert_not_called()
            self.assertEqual(rows,before)
        finally:
            panel._evaluate.clear()


if __name__=='__main__':unittest.main()
