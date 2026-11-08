"""Dated follow-up views are lazy, readable and never mutate scientific state."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import candidate_followup_panel as panel


STUDY = 'a'*64
SNAPSHOT = 'b'*64


def study_fixture():
    sizes, daily, weekly = [], [], []
    for size, groups in ((2,2),(4,1)):
        metrics=[dict(method=name,groups=0,loss=None,top1=0,top3=0) for name in panel.NAMES]
        sizes.append(dict(size=size,counts=dict(pending=groups),unique_matches=4,
                          closed_blocks=0,eligible_groups=0,metrics=deepcopy(metrics)))
        daily.append(dict(day='2026-10-25',size=size,groups=groups,unique_matches=4,
            pending=groups,settled=0,unresolved=0,void=0,before_kickoff=groups,late=0,min_lead_seconds=900))
        weekly.append(dict(week_utc='2026-10-19',size=size,groups=groups,unique_matches=4,
            pending=groups,settled=0,unresolved=0,void=0,calendar_closed=False,eligible_groups=0,
            metrics=deepcopy(metrics)))
    return dict(study_id=STUDY,league='39',registered_at='2026-10-25T00:00:00+00:00',
        rules_fingerprint='c'*64,implementation_compatible=True,groups=3,unique_matches=4,
        by_size=sizes,daily=daily,weekly=weekly,warnings=[])


def table(at, column):
    return next(frame.value for frame in at.dataframe if column in frame.value.columns)


class CandidateFollowupPanelTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        panel._read.clear();panel._past.clear()
        self.addCleanup(panel._read.clear);self.addCleanup(panel._past.clear)
        self.value=dict(version='candidate-followup-v1',as_of='2026-10-25T01:00:00+00:00',
            input_fingerprint='d'*64,source='API-Football',studies=[study_fixture()],warnings=[])
        self.saved=[dict(snapshot_id=SNAPSHOT,study_id=STUDY,observed_at='2026-10-25T00:30:00+00:00',
            utc_day='2026-10-25',groups=3,unique_matches=4,input_fingerprint='e'*64)]
        self.old=dict(snapshot_id=SNAPSHOT,study_id=STUDY,as_of='2026-10-25T00:30:00+00:00',
                     study=study_fixture(),utc_day='2026-10-25',input_fingerprint='e'*64)
        self.old['study']['by_size'][1]['counts']={'settled':1}
        self.old['study']['daily'][1].update(pending=0,settled=1)
        self.collect=self.stub(panel.observations,'collect',side_effect=lambda *a,**k:deepcopy(self.value))
        self.snapshots=self.stub(panel.observations,'snapshots',side_effect=lambda *a,**k:deepcopy(self.saved))
        self.loader=self.stub(panel.observations,'load_snapshot',side_effect=lambda *a,**k:deepcopy(self.old))
        self.client=self.stub(panel.api,'ApiFootballClient',side_effect=AssertionError('No API'))
        self.enroll=self.stub(panel.tracking,'register',side_effect=AssertionError('No enrollment'))
        self.run=self.stub(panel.tracking,'run_cycle',side_effect=AssertionError('No forecasts'))

    def stub(self, target, name, **kwargs):
        patcher=patch.object(target,name,**kwargs)
        value=patcher.start();self.addCleanup(patcher.stop)
        return value

    def app(self):
        return AppTest.from_string('from candidate_followup_panel import render\n'
            f'render({STUDY!r},root={str(self.root)!r})',default_timeout=15).run()

    def open(self,at):
        at.session_state['candidate_followup_'+STUDY]=True
        return at.run()

    def assert_no_actions(self):
        self.client.assert_not_called();self.enroll.assert_not_called();self.run.assert_not_called()
        self.assertEqual(list(self.root.iterdir()),[])

    def test_collapsed_panel_does_not_read_data_or_create_observations(self):
        at=self.app()
        self.assertFalse(at.exception)
        self.collect.assert_not_called();self.snapshots.assert_not_called();self.loader.assert_not_called()
        self.assertFalse(at.dataframe);self.assertFalse(at.get('download_button'))
        self.assert_no_actions()

    def test_open_and_size_period_filters_use_cached_read_only_counts(self):
        at=self.open(self.app())
        self.assertFalse(at.exception)
        daily=table(at,'Journée des matchs · Kinshasa')
        self.assertEqual(daily['En attente'].tolist(),[1])
        self.assertEqual(daily['Avance minimale (min)'].tolist(),[15])
        self.assertEqual(at.metric[0].value,'1')
        at.segmented_control(key='candidate_followup_size_'+STUDY).set_value(2)
        self.open(at)
        self.assertFalse(at.exception)
        self.assertEqual(table(at,'Journée des matchs · Kinshasa')['En attente'].tolist(),[2])
        at.segmented_control(key='candidate_followup_period_'+STUDY).set_value('weekly')
        self.open(at)
        self.assertFalse(at.exception)
        weekly=table(at,'Groupes admissibles aux blocs clos')
        self.assertEqual(weekly['Semaine calendaire close'].tolist(),[False])
        self.assertEqual(weekly['Groupes admissibles aux blocs clos'].tolist(),[0])
        self.assertEqual(len(table(at,'Groupes évalués observés')),4)
        self.assertEqual(self.collect.call_count,1);self.assertEqual(self.snapshots.call_count,1)
        self.loader.assert_not_called()
        self.assertTrue(any('n’est pas une prédiction perdue' in c.value for c in at.caption))
        self.assertTrue(any('pas un nouvel intervalle' in c.value for c in at.caption))
        self.assert_no_actions()

    def test_old_snapshot_can_be_read_without_rewriting_or_hiding_current(self):
        at=self.open(self.app())
        at.selectbox(key='candidate_followup_reading_'+STUDY).set_value(SNAPSHOT)
        self.open(at)
        self.assertFalse(at.exception)
        self.assertEqual(table(at,'Journée des matchs · Kinshasa')['Évalués'].tolist(),[1])
        self.loader.assert_called_once_with(SNAPSHOT,self.root.resolve())
        self.assertTrue(any(self.old['as_of'] in c.value for c in at.caption))
        at.selectbox(key='candidate_followup_reading_'+STUDY).set_value('current')
        self.open(at)
        self.assertFalse(at.exception)
        self.assertEqual(table(at,'Journée des matchs · Kinshasa')['En attente'].tolist(),[1])
        self.assertEqual(self.collect.call_count,1)
        self.assert_no_actions()

    def test_unavailable_observations_do_not_become_zero_results(self):
        self.collect.side_effect=ValueError('private detail must stay hidden')
        at=self.open(self.app())
        self.assertFalse(at.exception)
        self.assertFalse(at.metric);self.assertFalse(at.dataframe)
        self.assertTrue(any('indisponible ou invalide' in w.value for w in at.warning))
        self.assertFalse(any('private detail' in w.value for w in at.warning))
        self.assert_no_actions()

    def test_missing_snapshots_and_changed_engine_are_explicit_without_new_registration(self):
        self.saved=[]
        self.value['studies'][0]['implementation_compatible']=False
        at=self.open(self.app())
        self.assertFalse(at.exception)
        self.assertTrue(any('moteurs actuels diffèrent' in w.value for w in at.warning))
        self.assertTrue(any('Aucun relevé daté' in w.value for w in at.info))
        self.loader.assert_not_called()
        self.assert_no_actions()

    def test_methodology_notes_are_grouped_without_warning_alerts(self):
        self.value['warnings']=['Lecture seule : aucun entraînement.']
        self.value['studies'][0]['warnings']=['Les tailles partagent des rencontres.']
        at=self.open(self.app())
        self.assertFalse(at.exception)
        self.assertFalse(at.warning)
        notes=[c.value for c in at.caption if c.value.startswith('Règles de lecture :')]
        self.assertEqual(len(notes),1)
        self.assertIn('Les tailles partagent',notes[0])
        self.assert_no_actions()


if __name__=='__main__': unittest.main()
