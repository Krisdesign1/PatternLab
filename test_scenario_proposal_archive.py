import copy
from itertools import product
import unittest
from unittest.mock import patch

import prospective_models as prospective
import scenario_proposal_archive as archive


def method(count=5):
    positions = [{'1st': .21, 'Égal': .28, '2nd': .51},
                 {'1st': .23, 'Égal': .31, '2nd': .46}]
    distribution = dict(schema=prospective.DISTRIBUTION_VERSION, type='independent',
                        size=2, distributions=positions)
    ranking = sorted((dict(sequence=list(sequence), probability=
        positions[0][sequence[0]] * positions[1][sequence[1]])
        for sequence in product(archive.VALUES, repeat=2)),
        key=lambda row: (-row['probability'], tuple(row['sequence'])))[:count]
    return dict(version='saved-test-v1', ranking=ranking, distribution=distribution)


class ProposalArchiveTests(unittest.TestCase):
    def test_adaptive_short_or_long_set_keeps_reference_three_separate(self):
        for count in (1, 2, 4, 6):
            payload = method(count)
            payload['reference_ranking'] = method(3)['ranking']
            payload['proposal_policy'] = dict(version=archive.ADAPTIVE_POLICY,
                                               threshold=payload['ranking'][-1]['probability'])
            original = copy.deepcopy(payload)
            saved = archive.validate_method(payload, 2)
            self.assertEqual(saved, original)
            self.assertEqual(len(saved['ranking']), count)
            self.assertEqual(len(saved['reference_ranking']), 3)
            info = archive.manifest({'clubs': saved}, policy=archive.ADAPTIVE_POLICY)
            with patch.object(prospective, '_rank', side_effect=AssertionError('No ranking on read')):
                self.assertEqual(archive.validate_manifest({'clubs': saved}, info), info)

    def test_adaptive_rows_cannot_breach_the_saved_threshold_or_frozen_model(self):
        original = method(4)
        original['reference_ranking'] = method(3)['ranking']
        original['proposal_policy'] = dict(version=archive.ADAPTIVE_POLICY,
                                            threshold=original['ranking'][-1]['probability'])
        for change in ('threshold', 'probability', 'reference', 'version', 'boolean', 'absent_policy'):
            payload = copy.deepcopy(original)
            if change == 'threshold': payload['proposal_policy']['threshold'] += .001
            elif change == 'probability': payload['ranking'][-1]['probability'] += .0001
            elif change == 'reference': payload['reference_ranking'].pop()
            elif change == 'version': payload['proposal_policy']['version'] = 'unknown'
            elif change == 'boolean': payload['proposal_policy']['threshold'] = True
            else: payload.pop('proposal_policy')
            with self.subTest(change=change), self.assertRaises(ValueError):
                archive.validate_method(payload, 2)

    def test_keeps_every_explicit_choice_without_current_model_or_generation(self):
        payload = method(9)
        before = copy.deepcopy(payload)
        with patch.object(prospective, 'build_methods', side_effect=AssertionError('No fit')):
            saved = archive.validate_method(payload, 2)
        self.assertEqual(len(saved['ranking']), 9)
        self.assertEqual(saved, before)
        saved['ranking'][3]['sequence'][0] = 'changed'
        self.assertEqual(payload, before)

    def test_does_not_turn_unsaved_distribution_outcomes_into_proposals(self):
        saved = archive.validate_methods({'clubs': method(3)})
        info = archive.manifest(saved, policy='top3_per_method')
        self.assertEqual(len(saved['clubs']['ranking']), 3)
        self.assertEqual(info['proposal_counts'], {'clubs': 3})
        self.assertEqual(info['distinct_proposal_count'], 3)
        self.assertEqual(info['policy'], 'top3_per_method')
        self.assertEqual(info['scope'], 'explicit_saved_rankings')

    def test_legacy_ranking_only_accepts_one_two_and_more_than_three_choices(self):
        for count in (1, 2, 5, 9):
            payload = method(count)
            del payload['distribution']
            self.assertEqual(len(archive.validate_method(payload, 2)['ranking']), count)

    def test_invalid_tail_is_not_silently_dropped(self):
        original = method(5)
        variants = []
        bad = copy.deepcopy(original); bad['ranking'][4]['probability'] += .001; variants.append(bad)
        bad = copy.deepcopy(original); bad['ranking'][4] = copy.deepcopy(bad['ranking'][3]); variants.append(bad)
        bad = copy.deepcopy(original); bad['ranking'][4]['sequence'] = ['invalid']; variants.append(bad)
        bad = copy.deepcopy(original); bad['ranking'][4]['probability'] = float('nan'); variants.append(bad)
        bad = copy.deepcopy(original); bad['ranking'][3:5] = reversed(bad['ranking'][3:5]); variants.append(bad)
        for payload in variants:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                archive.validate_method(payload, 2)

    def test_original_canonical_choices_still_use_frozen_validator(self):
        payload = method(5)
        payload['ranking'][0:2] = reversed(payload['ranking'][0:2])
        with self.assertRaises(ValueError):
            archive.validate_method(payload, 2)
        payload = method(5)
        payload['distribution']['distributions'][0]['1st'] = .5
        with self.assertRaises(ValueError):
            archive.validate_method(payload, 2)

    def test_summary_only_still_rejects_mass_order_duplicates_and_mixed_size(self):
        for change in ('mass', 'order', 'duplicate', 'size', 'boolean'):
            payload = method(5)
            del payload['distribution']
            if change == 'mass':
                for row in payload['ranking']: row['probability'] = .5
            elif change == 'order': payload['ranking'][4]['probability'] = .99
            elif change == 'duplicate': payload['ranking'][4]['sequence'] = payload['ranking'][0]['sequence']
            elif change == 'size': payload['ranking'][4]['sequence'].append('1st')
            else: payload['ranking'][4]['probability'] = True
            with self.subTest(change=change), self.assertRaises(ValueError):
                archive.validate_methods({'clubs': payload})

    def test_manifest_preserves_union_and_sha_is_independent_of_mapping_order(self):
        methods = {'clubs': method(5), 'combined': method(3)}
        before = copy.deepcopy(methods)
        info = archive.manifest(methods, unavailable={'catboost': 'Modèle absent.'})
        self.assertEqual(info['saved_proposal_count'], 8)
        self.assertEqual(info['distinct_proposal_count'], 5)
        self.assertEqual(info, archive.manifest(dict(reversed(list(methods.items()))),
                         unavailable={'catboost': 'Modèle absent.'}))
        self.assertEqual(archive.validate_manifest(methods, info), info)
        self.assertEqual(methods, before)

    def test_manifest_reads_never_validate_rank_or_score_a_model(self):
        methods = {'clubs': method(5), 'combined': method(3)}
        info = archive.manifest(methods)
        with patch.object(prospective, 'validate_method', side_effect=AssertionError('No validation')), \
             patch.object(prospective, '_rank', side_effect=AssertionError('No ranking')), \
             patch.object(prospective, 'probability_for_sequence', side_effect=AssertionError('No scoring')):
            self.assertEqual(archive.validate_manifest(methods, info), info)

    def test_only_additional_saved_choices_need_individual_probability_scoring(self):
        operation = prospective.probability_for_sequence
        with patch.object(prospective, 'probability_for_sequence', wraps=operation) as score:
            archive.validate_method(method(3), 2)
            score.assert_not_called()
            payload = method(5)
            archive.validate_method(payload, 2)
            self.assertEqual(score.call_count, 2)
            self.assertEqual([call.args[1] for call in score.call_args_list],
                             [row['sequence'] for row in payload['ranking'][3:]])

    def test_top_three_policy_cannot_attest_another_proposal_count(self):
        for count in (1, 2, 4, 9):
            methods = {'clubs': method(count)}
            # Short legacy rankings do not have the current full distribution.
            methods['clubs'].pop('distribution', None)
            with self.subTest(count=count), self.assertRaises(ValueError):
                archive.manifest(methods, policy='top3_per_method')
            info = archive.manifest(methods)
            info['policy'] = 'top3_per_method'
            with self.assertRaises(ValueError):
                archive.validate_manifest(methods, info)

    def test_manifest_detects_changed_tail_counts_model_metadata_and_unavailable_reasons(self):
        methods = {'clubs': method(5)}
        info = archive.manifest(methods, unavailable={'catboost': 'Absent.'})
        for change in ('count', 'tail', 'metadata', 'unavailable', 'digest', 'policy', 'extra'):
            saved, changed = copy.deepcopy(methods), copy.deepcopy(info)
            if change == 'count': changed['distinct_proposal_count'] = 3
            elif change == 'tail': saved['clubs']['ranking'].pop()
            elif change == 'metadata': saved['clubs']['version'] = 'other-version'
            elif change == 'unavailable': changed['unavailable']['catboost'] = 'Autre motif.'
            elif change == 'digest': changed['sha256'] = '0' * 64
            elif change == 'policy': changed['policy'] = 'other-policy'
            else: changed['unexpected'] = True
            with self.subTest(change=change), self.assertRaises(ValueError):
                archive.validate_manifest(saved, changed)

    def test_rejects_absent_methods_and_contradictory_unavailable_method(self):
        for payload in ({}, None, {'': method(3)}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                archive.validate_methods(payload)
        with self.assertRaises(ValueError):
            archive.manifest({'clubs': method(3)}, unavailable={'clubs': 'Absent.'})


if __name__ == '__main__':
    unittest.main()
