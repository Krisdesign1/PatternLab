"""The proposal UI preserves supplied choices; evidence never hides a group."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import unittest

from streamlit.testing.v1 import AppTest

import adaptive_scenario_proposals as proposals
from adaptive_scenario_calibration import VERSION, context_key, digest
from test_scenario_proposal_archive import method


NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


def entry_with_choices(count=5, *, evidence=False):
    methods = {'clubs': method(count), 'combined': method(3)}
    fixtures = [dict(match_id=str(100+index), provider='api-football', league='39',
                     home=f'Home{index}', away=f'Away{index}',
                     timestamp=f'2026-11-07T19:0{index}:00+00:00') for index in range(2)]
    artifact = dict(schema=VERSION, created_at='2026-11-07T11:00:00+00:00', contexts={})
    if evidence:
        for name, payload in methods.items():
            artifact['contexts'][context_key(name, payload, 2)] = dict(
                status='not_qualified', reason='Échantillon historique encore insuffisant.',
                tuning_groups=12, test_groups=7, tuning_days=2, test_days=1)
    artifact['sha256'] = digest(artifact)
    selected = proposals.capture(methods, fixtures, now=NOW, artifact=artifact)
    return dict(id='test-computed', source='API-Football', created_at=NOW.isoformat(),
                size=2, fixtures=fixtures, methods=methods, proposal_selection=selected)


def render_entry(entry):
    return ('import json\nimport adaptive_scenario_proposals as proposals\n'
            'import scenario_tables as tables\n'
            f'entry=json.loads({json.dumps(json.dumps(entry))})\n'
            'tables.render_archive_scope(entry)\n'
            'tables.render_matrix(proposals.effective_methods(entry), key="computed")\n')


class ProposalNoGateUiTests(unittest.TestCase):
    def test_all_supplied_choices_visible_without_qualified_artifact(self):
        entry = entry_with_choices(5)
        before = deepcopy(entry)
        at = AppTest.from_string(render_entry(entry), default_timeout=15).run()
        self.assertFalse(at.exception)
        self.assertFalse(at.warning)
        self.assertFalse(at.info)
        captions = '\n'.join(item.value for item in at.caption)
        self.assertIn('Clubs : 5 choix', captions)
        self.assertIn('2 à 20 matchs', captions)
        self.assertIn('5 scénarios distincts conservés', captions)
        self.assertNotIn('seuil', captions.lower())
        self.assertIn('ne bloque aucune taille de groupe', captions)
        self.assertEqual(at.dataframe[0].value['Propositions enregistrées'].tolist(), [5, 3])
        self.assertEqual(at.dataframe[1].value['Scénarios retenus'].tolist(), [5, 3])
        self.assertEqual([item.label for item in at.number_input], ['Page du classement complet'])
        self.assertFalse(at.slider)
        self.assertEqual(entry, before)

    def test_evidence_failure_is_an_annotation_instead_of_an_empty_table(self):
        entry = entry_with_choices(4, evidence=True)
        at = AppTest.from_string(render_entry(entry), default_timeout=15).run()
        self.assertFalse(at.exception)
        self.assertFalse(at.info)
        self.assertEqual(at.dataframe[0].value['Comparaison historique'].tolist(),
                         ['Critère historique non atteint']*2)
        self.assertEqual(at.dataframe[0].value['Groupes de contrôle'].tolist(), [7, 7])
        captions = '\n'.join(item.value for item in at.caption)
        self.assertIn('4 scénarios distincts conservés', captions)
        self.assertNotIn('sans nombre de scénarios imposé', captions)

    def test_initial_manual_comparison_uses_the_same_complete_supplied_set(self):
        entry = entry_with_choices(5, evidence=True)
        # The selected engine may itself supply more than its three reference
        # rows. Its initial screen must keep that tail too.
        entry['methods']['combined'] = method(5)
        entry['proposal_selection'] = proposals.capture(entry['methods'], entry['fixtures'], now=NOW, artifact={})
        payload = dict(engine='statistical', source='API-Football', fixtures=entry['fixtures'],
                       forecast={'ranking': entry['methods']['combined']['ranking'][:3]},
                       comparison_methods=entry['methods'], proposal_selection=entry['proposal_selection'])
        source = ('import json\nimport scenario_presentation as presentation\n'
                  f'payload=json.loads({json.dumps(json.dumps(payload))})\n'
                  'presentation.render_analysis_matrix(payload)\n')
        at = AppTest.from_string(source, default_timeout=15).run()
        self.assertFalse(at.exception)
        self.assertFalse(at.warning)
        self.assertFalse(at.info)
        captions = '\n'.join(item.value for item in at.caption)
        self.assertIn('Les distributions complètes et leurs classements sont sauvegardés', captions)
        self.assertIn('5 scénarios distincts conservés', captions)
        self.assertEqual(at.dataframe[0].value['Propositions enregistrées'].tolist(), [5, 5])


if __name__ == '__main__':
    unittest.main()
