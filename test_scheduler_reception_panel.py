from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import scheduler_reception_panel as panel


def sample():
    row = dict(protocol_id='a'*64,day='2026-11-02',warmup_at='2026-11-02T07:30:00+00:00',
        decision_at='2026-11-02T08:00:00+00:00',deadline_at='2026-11-02T08:30:00+00:00',state='upcoming',
        target_count=None,capture_count=0,timing_admissible_count=0,missing_known_targets=None,
        first_capture_ack_at=None,last_capture_ack_at=None,closure_at=None,closure_ack_at=None,
        closure_delay_seconds=None,registration_ready=True,warmup_observed_at=None,issues=[])
    return dict(version='scheduler-reception-v1',as_of='2026-11-01T18:00:00+00:00',
        deadlines=dict(source_sha256='b'*64,protocols=[dict(id='a'*64,start='2026-11-02T00:00:00+00:00',
            end='2026-12-28T00:00:00+00:00',implementation_compatible=True)],days=[row]),
        operations=dict(state='available',truncated=False,stale_threshold_seconds=1800,
            latest_operations={phase:dict(state='missing',observed_at=None,day=None,age_seconds=None)
                               for phase in panel.PHASES},resumptions=[],cycles=[],issues=[],source_sha256='c'*64),
        atomic_across_databases=False,predictive_improvement_demonstrated=False,availability_continuous_proven=False)


class SchedulerReceptionPanelTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        p = patch.object(panel,'collect',return_value=sample());self.collect=p.start();self.addCleanup(p.stop)
        self.at = AppTest.from_string(f"from scheduler_reception_panel import render\nrender({str(self.root)!r})",
                                     default_timeout=15)

    def read(self):
        self.at.run().button(key='scheduler_reception_read').click().run()
        self.assertFalse(self.at.exception)
        return self.at

    def table(self,column):
        return next(t.value for t in self.at.dataframe if column in t.value.columns)

    def test_open_is_lazy_no_collect_and_no_storage_create(self):
        self.at.run()
        self.assertFalse(self.at.exception);self.collect.assert_not_called()
        self.assertFalse(self.at.dataframe);self.assertFalse(list(self.root.iterdir()))

    def test_upcoming_has_no_false_success_and_kinshasa_schedule(self):
        self.read()
        self.assertFalse(self.at.success)
        self.assertTrue(any('Aucune fenêtre échue' in x.value for x in self.at.info))
        self.assertEqual(self.table('Décision · Kinshasa').iloc[0]['Décision · Kinshasa'],'02/11/2026 09:00:00')
        self.assertEqual(self.table('Fin de capture · Kinshasa').iloc[0]['Fin de capture · Kinshasa'],'02/11/2026 09:30:00')
        self.assertTrue(self.table('Cibles recensées').iloc[0]['Cibles recensées'] is None)
        self.assertTrue(any('Prochaine décision inscrite' in x.value for x in self.at.caption))
        self.assertEqual(self.table('Code installé').iloc[0]['Code installé'],'Compatible')

    def test_incompatible_period_keeps_registered_future_states_without_capture_promise(self):
        value=sample();value['deadlines']['protocols'][0]['implementation_compatible']=False
        self.collect.return_value=value
        self.read()
        self.assertTrue(any('Code incompatible — consultation uniquement' in n.value for n in self.at.warning))
        self.assertFalse(any('Prochaine décision inscrite' in n.value for n in self.at.caption))
        self.assertEqual(self.table('État').iloc[0]['État'],'À venir')
        self.assertEqual(self.table('Code installé').iloc[0]['Code installé'],'Code incompatible')

    def test_missing_compatibility_is_unknown_not_implicitly_compatible(self):
        value=sample();value['deadlines']['protocols'][0].pop('implementation_compatible')
        self.collect.return_value=value
        self.read()
        self.assertTrue(any('Compatibilité non vérifiée — consultation uniquement' in n.value for n in self.at.warning))
        self.assertFalse(any('Prochaine décision inscrite' in n.value for n in self.at.caption))

    def test_old_incompatible_and_new_compatible_periods_remain_separate(self):
        value=sample();old=value['deadlines']['protocols'][0];old['implementation_compatible']=False
        new=deepcopy(old);new.update(id='d'*64,start='2026-11-09T00:00:00+00:00',
            end='2027-01-04T00:00:00+00:00',implementation_compatible=True)
        new_day=deepcopy(value['deadlines']['days'][0]);new_day.update(protocol_id=new['id'],day='2026-11-09',
            warmup_at='2026-11-09T07:30:00+00:00',decision_at='2026-11-09T08:00:00+00:00',
            deadline_at='2026-11-09T08:30:00+00:00')
        value['deadlines']['protocols'].append(new);value['deadlines']['days'].append(new_day)
        self.collect.return_value=value
        self.read()
        self.assertEqual(self.table('Journée UTC').iloc[0]['Journée UTC'],'2026-11-09')
        self.assertTrue(any('Prochaine décision inscrite' in n.value for n in self.at.caption))
        self.at.selectbox(key='scheduler_reception_protocol').set_value(old['id']).run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.table('Journée UTC').iloc[0]['Journée UTC'],'2026-11-02')
        self.assertTrue(any('consultation uniquement' in n.value for n in self.at.warning))
        self.assertFalse(any('Prochaine décision inscrite' in n.value for n in self.at.caption))
        self.assertEqual(self.collect.call_count,1)

    def test_missed_census_keeps_unknown_not_zero_matches(self):
        value=sample();value['deadlines']['days'][0].update(state='missed',issues=['unknown_census'])
        self.collect.return_value=value
        self.read()
        self.assertTrue(any('nombre de matchs manqués est inconnu' in n.value for n in self.at.warning))
        self.assertEqual(self.table('État').iloc[0]['État'],'Recensement manqué')

    def test_closure_and_missing_captures_stay_separate_in_same_table(self):
        value=sample();row=value['deadlines']['days'][0]
        row.update(state='closed_unverified',target_count=10,capture_count=8,timing_admissible_count=7,
                   missing_known_targets=2,issues=['closure_timing_unverified','capture_timing_unverified'])
        self.collect.return_value=value
        self.read()
        table=self.table('Captures conservées')
        self.assertEqual(table.iloc[0]['Captures conservées'],8)
        self.assertEqual(table.iloc[0]['Captures attestées à temps'],7)
        self.assertIn('Accusé de fermeture absent',table.iloc[0]['Points à vérifier'])
        self.assertFalse(self.at.success)

    def test_stale_calendar_and_resume_do_not_claim_sleep_or_good_uptime(self):
        value=sample();op=value['operations']
        op['latest_operations']['calendar'].update(state='stale',observed_at='2026-11-01T00:00:00+00:00',
                                                  day='2026-11-01',age_seconds=64800)
        op['resumptions']=[dict(cycle_id='d'*32,previous_at='2026-11-01T00:00:00+00:00',
            resumed_at='2026-11-01T17:00:00+00:00',gap_seconds=61200,resumed_phase='results',
            calendar_at='2026-11-01T00:00:00+00:00',calendar_age_seconds=61200,calendar_state='stale')]
        self.collect.return_value=value;self.read()
        self.assertTrue(any('données sont anciennes' in w.value for w in self.at.warning))
        self.assertEqual(self.table('Ancienneté du calendrier à la reprise (min)').iloc[0]
                         ['Ancienneté du calendrier à la reprise (min)'],1020)
        self.assertFalse(self.at.success)

    def test_failed_new_read_removes_stale_report_and_export(self):
        self.read();self.assertTrue(self.at.dataframe)
        self.collect.side_effect=panel.deadlines.DeadlineError('private response')
        self.at.button(key='scheduler_reception_read').click().run()
        self.assertFalse(self.at.exception);self.assertTrue(self.at.error)
        self.assertFalse(self.at.dataframe);self.assertFalse(self.at.get('download_button'))
        self.assertNotIn('private response',str([x.value for x in self.at.error]))

    def test_filters_and_export_do_not_recollect_or_change_sources(self):
        with patch.object(panel.st,'download_button',wraps=panel.st.download_button) as download:
            self.read();self.at.run()
        self.assertEqual(self.collect.call_count,1)
        exported=json.loads(download.call_args.args[1])
        self.assertEqual(exported['deadlines']['source_sha256'],'b'*64)
        self.assertEqual(exported['operations']['source_sha256'],'c'*64)
        self.assertFalse(exported['atomic_across_databases'])
        self.assertFalse(exported['availability_continuous_proven'])

    def test_invalid_operations_report_does_not_hide_deadlines(self):
        value=sample();value['operations']['state']='unavailable_or_invalid';self.collect.return_value=value
        self.read()
        self.assertTrue(self.table('Journée UTC').shape[0])
        self.assertTrue(any('journal des opérations est absent ou invalide' in n.value for n in self.at.warning))
        self.assertFalse(any('Cycle' in t.value.columns for t in self.at.dataframe))

    def test_clock_issue_codes_are_translated_and_remain_visible(self):
        value=sample();value['operations']['issues']=['future_signal'];self.collect.return_value=value
        self.read()
        self.assertEqual(self.table('Point à vérifier').iloc[0]['Point à vérifier'],'Signal dans le futur')
        self.assertFalse(self.at.success)

    def test_collect_only_reads_two_reports_with_same_cutoff(self):
        real = collect_function
        with patch.object(panel.deadlines,'load',return_value={}) as first, \
             patch.object(panel.operations,'load',return_value={}) as second:
            value=real(self.root,interval_minutes=7)
        self.assertEqual(first.call_args.kwargs['now'],second.call_args.kwargs['now'])
        self.assertEqual(second.call_args.kwargs['interval_minutes'],7)
        self.assertFalse(value['predictive_improvement_demonstrated'])


collect_function=panel.collect


if __name__=='__main__':
    unittest.main()
