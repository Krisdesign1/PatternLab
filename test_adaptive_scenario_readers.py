"""Every consumer reads one frozen adaptive set, independent of today's policy."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import adaptive_scenario_proposals as proposals
from adaptive_scenario_calibration import VERSION, context_key, digest
import bet_simulation
import group_probability_diagnostics as diagnostics
import scenario_outcomes
import scenario_tables
from test_scenario_proposal_archive import method


NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


def saved(threshold=.11):
    core = {'clubs': method(3)}
    metrics = dict(groups=20, hits=8, proposals=60, precision=8/60, f05=10/65,
                   coverage=.4, mean_proposals=3, maximum_proposals=4, abstentions=0)
    context = dict(status='qualified', threshold=threshold,
                   tuning_groups=30, test_groups=20, tuning_days=3, test_days=3,
                   tuning=deepcopy(metrics), test=deepcopy(metrics), reference_top3=deepcopy(metrics),
                   test_first_day='2026-11-01', tuning_labels_before='2026-11-01T00:00:00+00:00')
    artifact = dict(schema=VERSION, created_at='2026-11-07T11:00:00+00:00',
                    contexts={context_key('clubs', core['clubs'], 2): context})
    artifact['sha256'] = digest(artifact)
    fixtures = [dict(match_id=str(100+index), provider='api-football', league='39',
                     home=f'Home{index}', away=f'Away{index}',
                     timestamp=f'2026-11-07T19:0{index}:00+00:00') for index in range(2)]
    envelope = proposals.legacy_capture(core, fixtures, now=NOW, artifact=artifact)
    return dict(id='test-adaptive', source='API-Football', created_at=NOW.isoformat(),
                size=2, fixtures=fixtures, methods=core, proposal_selection=envelope,
                checked_at='2026-11-08T01:00:00+00:00', status='settled', actual=['2nd', '2nd'])


class AdaptiveScenarioReaderTests(unittest.TestCase):
    def test_one_two_or_four_choices_reach_matrix_verdict_and_simulation_identically(self):
        for threshold, count in ((.23, 1), (.15, 2), (.11, 4)):
            entry = saved(threshold)
            expected = entry['proposal_selection']['methods']['clubs']['ranking']
            entry['actual'] = deepcopy(expected[-1]['sequence'])
            before = deepcopy(entry)
            with self.subTest(count=count), \
                 patch.object(proposals, 'load_artifact', side_effect=AssertionError('No current policy')), \
                 patch.object(proposals, 'iter_selected', side_effect=AssertionError('No generation on read')):
                effective = proposals.effective_methods(entry)
                matrix = scenario_tables.build_matrix(effective, actual=entry['actual'])
                outcome = scenario_outcomes.summarize(entry)
                ledger = bet_simulation.build([entry], now=datetime(2026,11,8,2,tzinfo=timezone.utc))
            self.assertEqual(len(effective['clubs']['ranking']), count)
            self.assertEqual(len(matrix['rows']), count)
            self.assertEqual([list(row['sequence']) for row in matrix['rows']],
                             [row['sequence'] for row in expected])
            self.assertEqual(outcome['state'], 'hit')
            self.assertEqual(outcome['matches'][0]['rank'], count)
            self.assertEqual((ledger['summary']['bets'], ledger['summary']['won']), (count, 1))
            self.assertEqual(sorted(ticket['sequence'] for ticket in ledger['tickets']),
                             sorted(row['sequence'] for row in expected))
            reference = diagnostics.inspect(entry['methods']['clubs'])
            selected = diagnostics.inspect(effective['clubs'])
            self.assertEqual(selected['top3_probability'], reference['top3_probability'])
            self.assertEqual(entry, before)

    def test_unqualified_selection_never_becomes_reference_fallback_false_failure_or_stakes(self):
        entry = saved(1)
        self.assertEqual(entry['proposal_selection']['methods'], {})
        for state in ('pending', 'settled'):
            entry['status'] = state
            result = scenario_outcomes.summarize(entry)
            self.assertEqual(result['state'], 'unavailable')
            self.assertEqual(result['reason'], 'selection_abstained')
            self.assertEqual(result['label'], 'Aucun scénario retenu par la sélection')
        ledger = bet_simulation.build([entry], now=datetime(2026,11,8,2,tzinfo=timezone.utc))
        self.assertEqual(ledger['summary']['bets'], 0)
        self.assertEqual(ledger['exclusions'], {'selection_abstained': 1})

    def test_truncated_envelope_cannot_supply_a_false_miss_or_partial_simulation(self):
        entry = saved()
        entry['proposal_selection']['methods']['clubs']['ranking'].pop()
        self.assertEqual(scenario_outcomes.summarize(entry)['state'], 'unavailable')
        ledger = bet_simulation.build([entry], now=datetime(2026,11,8,2,tzinfo=timezone.utc))
        self.assertEqual(ledger['summary']['bets'], 0)

    def test_ui_explains_selection_count_threshold_and_reference_without_count_setting(self):
        entry = saved()
        source = ('import json\nimport adaptive_scenario_proposals as proposals\n'
                  'import scenario_tables as tables\n'
                  f'entry=json.loads({json.dumps(json.dumps(entry))})\n'
                  'tables.render_archive_scope(entry)\n'
                  'tables.render_matrix(proposals.effective_methods(entry), actual=entry["actual"], key="adaptive")\n')
        at = AppTest.from_string(source, default_timeout=15).run()
        self.assertFalse(at.exception)
        captions = '\n'.join(item.value for item in at.caption)
        self.assertIn('sans nombre de scénarios imposé', captions)
        self.assertIn('Clubs : 4 choix', captions)
        self.assertIn('seuil 11,0000 %', captions)
        self.assertIn('La référence des trois premiers reste séparée', captions)
        self.assertEqual(at.dataframe[0].value['Scénarios retenus'].tolist(), [4])
        self.assertEqual([item.label for item in at.number_input], ['Page du classement complet'])
        self.assertFalse(at.slider)


if __name__ == '__main__':
    unittest.main()
