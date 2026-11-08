"""Native Streamlit UI checks for live collection monitoring."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


def view(**changes):
    result=dict(exists=True, status='collecting', total=100, resolved=40,
        remaining=60, ready=35, partial=2, empty=3, invalid=0, retry_errors=0,
        last_activity_utc='2026-10-26T20:00:00+00:00', last_activity_age_seconds=2,
        rate_per_hour=3600, eta_seconds=60, estimated_finish_utc='2026-10-26T20:01:00+00:00',
        provider_pause_until=0, provider_remaining_day=1000,
        per_year=[dict(year=2024,total=100,resolved=40,remaining=60,ready=35)],
        comparison=dict(stage='waiting_collection',prepared_examples=6065,detail='Collecte encore incomplète.'))
    result.update(changes)
    return result


class CollectionPanelTests(unittest.TestCase):
    def setUp(self):
        import halftime_collection_panel as panel
        self.panel=panel
        panel._load_status.clear()
        panel._load_compact_status.clear()
        self.addCleanup(panel._load_status.clear)
        self.addCleanup(panel._load_compact_status.clear)
        self.current=view()
        stub=patch.object(panel.status_source,'load_status',side_effect=lambda **kwargs:deepcopy(self.current))
        self.load=stub.start();self.addCleanup(stub.stop)
    def open(self):
        return AppTest.from_string('from halftime_collection_panel import render_collection\nrender_collection()',default_timeout=10).run()
    def test_live_progress_counts_eta_and_local_time(self):
        at=self.open()
        self.assertFalse(at.exception)
        metrics={node.label:node.value for node in at.metric}
        self.assertEqual(metrics['Réponses enregistrées'],'40')
        self.assertEqual(metrics['Matchs restants'],'60')
        self.assertEqual(metrics['Tirs complets par mi-temps'],'35')
        self.assertTrue(any('21:01' in node.value for node in at.info))
        self.assertEqual(len(at.dataframe[0].value),1)
        self.assertTrue(any('15 secondes' in node.value for node in at.caption))
    def test_pause_hides_old_eta(self):
        self.current=view(status='paused',provider_pause_until=1793050200)
        at=self.open()
        self.assertFalse(at.exception)
        self.assertTrue(at.warning)
        self.assertFalse(any('Fin estimée' in node.value for node in at.info))
    def test_inactive_hides_old_eta_and_explains_uncertainty(self):
        self.current=view(status='inactive')
        at=self.open()
        self.assertFalse(at.exception)
        self.assertTrue(any('peut être arrêté' in node.value for node in at.warning))
        self.assertFalse(any('Fin estimée' in node.value for node in at.info))
    def test_completion_notice_once_and_comparison_stays_distinct(self):
        with patch.object(self.panel.st,'toast',wraps=self.panel.st.toast) as toast:
            at=self.open()
            self.assertEqual(toast.call_count,0)
            self.current=view(status='complete',resolved=100,remaining=0,ready=70,partial=10,empty=20,
                              comparison=dict(stage='collection_ready',prepared_examples=6065))
            at.button(key='halftime_collection_refresh').click().run()
            self.assertFalse(at.exception)
            self.assertTrue(any('Collecte terminée' in node.value for node in at.success))
            self.assertTrue(any('reste à exécuter' in node.value for node in at.info))
            self.assertTrue(at.session_state['halftime_collection_completion:default'])
            self.assertEqual(toast.call_count,1)
            at.button(key='halftime_collection_refresh').click().run()
            self.assertFalse(at.exception)
            self.assertTrue(at.session_state['halftime_collection_completion:default'])
            self.assertEqual(toast.call_count,1)
    def test_missing_collection_is_clear_without_metrics(self):
        self.current=dict(exists=False,status='not_started')
        at=self.open()
        self.assertFalse(at.exception)
        self.assertTrue(any('Aucune collecte' in node.value for node in at.info))
        self.assertFalse(at.metric)
    def test_unavailable_is_nonfatal(self):
        self.current=dict(exists=True,status='unavailable')
        at=self.open()
        self.assertFalse(at.exception)
        self.assertTrue(at.warning)
        self.assertFalse(at.metric)
    def test_empty_and_partial_do_not_claim_richness_for_all_responses(self):
        self.current=view(status='complete',resolved=100,remaining=0,ready=0,empty=80,partial=20,
                          comparison=dict(stage='not_prepared'))
        at=self.open()
        self.assertFalse(at.exception)
        self.assertEqual(next(n.value for n in at.metric if n.label=='Tirs complets par mi-temps'),'0')
        self.assertTrue(any('80 réponses vides' in n.value for n in at.caption))
        self.assertTrue(any('ne signifie pas' in n.value for n in at.caption))
    def test_compact_missing_does_not_render_or_create_files(self):
        self.current=dict(exists=False,status='not_started')
        with tempfile.TemporaryDirectory() as temp:
            at=AppTest.from_string('import streamlit as st\nfrom halftime_collection_panel import render_compact\n'
                                  f'with st.sidebar:\n    render_compact(root={temp!r})',default_timeout=10).run()
            self.assertFalse(at.exception)
            self.assertFalse(at.progress if hasattr(at,'progress') else at.get('progress'))
            self.assertEqual(list(Path(temp).iterdir()),[])


if __name__=='__main__':unittest.main()
