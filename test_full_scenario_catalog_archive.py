"""Compact complete catalogues survive archival without enlarging success sets."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import unittest

from adaptive_scenario_calibration import digest
import adaptive_scenario_proposals as proposals
import manual_proposal_archive as manual
import prospective_models as models
import scenario_proposal_archive as archive


NOW = datetime(2026, 11, 8, 8, tzinfo=timezone.utc)


def method(size=10, count=3):
    distribution = models._distribution('independent', size,
        distributions=[{'1st': .21, 'Égal': .28, '2nd': .51} for _ in range(size)])
    return dict(version='catalogue-archive-test-v1', distribution=distribution,
                ranking=models._rank(models._read_distribution(distribution), count))


def fixtures(size=10):
    return [dict(match_id=str(100 + index), timestamp='2026-11-09T15:00:00+00:00')
            for index in range(size)]


def rehash(value):
    value['sha256'] = digest({key: item for key, item in value.items() if key != 'sha256'})
    return value


class FullScenarioCatalogueArchiveTests(unittest.TestCase):
    def test_new_write_attests_59049_choices_without_materializing_them(self):
        core = {'clubs': method()}
        before = deepcopy(core)
        saved = archive.with_catalogs(core, 10)
        self.assertEqual(saved['clubs']['scenario_catalog']['total'], 59049)
        self.assertEqual(saved['clubs']['ranking'], core['clubs']['ranking'])
        self.assertLess(len(json.dumps(saved)), 6000)
        self.assertEqual(core, before)
        info = archive.manifest(saved)
        self.assertEqual(info['catalogue_scope'], 'all_frozen_distribution_outcomes')
        self.assertEqual(info['proposal_counts'], {'clubs': 3})
        self.assertEqual(archive.validate_manifest(saved, info), info)

    def test_v3_capture_and_reread_preserve_compact_catalogue_and_fourth_choice(self):
        core = {'clubs': method(count=5)}
        frozen = proposals.capture(core, fixtures(), now=NOW, artifact={})
        self.assertEqual(frozen['schema'], proposals.VERSION)
        self.assertEqual(frozen['methods']['clubs']['scenario_catalog']['total'], 59049)
        self.assertEqual(len(frozen['methods']['clubs']['ranking']), 5)
        proposals.validate(frozen, core, fixtures(), NOW.isoformat())
        entry = dict(methods=core, proposal_selection=frozen, fixtures=fixtures(),
                     created_at=NOW.isoformat())
        effective = proposals.effective_methods(entry)
        self.assertEqual(effective['clubs']['ranking'][3], core['clubs']['ranking'][3])
        self.assertEqual(effective['clubs']['scenario_catalog'],
                         frozen['methods']['clubs']['scenario_catalog'])
        archive.validate_methods(effective, 10)

    def test_rehashed_catalogue_omission_or_count_change_is_rejected(self):
        core = {'clubs': method()}
        original = proposals.capture(core, fixtures(), now=NOW, artifact={})
        for change in ('absent', 'total', 'digest'):
            saved = deepcopy(original)
            if change == 'absent': saved['methods']['clubs'].pop('scenario_catalog')
            elif change == 'total': saved['methods']['clubs']['scenario_catalog']['total'] = 3
            else: saved['methods']['clubs']['scenario_catalog']['distribution_sha256'] = '0' * 64
            with self.subTest(change=change), self.assertRaises(ValueError):
                proposals.validate(rehash(saved), core, fixtures())

    def test_v2_and_ranking_only_legacy_are_not_supplemented_on_read(self):
        core = {'clubs': method()}
        saved = proposals.capture(core, fixtures(), now=NOW, artifact={})
        saved['schema'] = proposals.COMPUTED_V2
        saved['methods']['clubs'].pop('scenario_catalog')
        rehash(saved)
        effective = proposals.effective_methods(dict(methods=core, proposal_selection=saved,
            fixtures=fixtures(), created_at=NOW.isoformat()))
        self.assertNotIn('scenario_catalog', effective['clubs'])
        self.assertNotIn('catalogue_scope', archive.manifest(effective))
        summary = method(); summary.pop('distribution')
        frozen = proposals.capture({'clubs': summary}, fixtures(), now=NOW, artifact={})
        self.assertNotIn('scenario_catalog', frozen['methods']['clubs'])
        proposals.validate(frozen, {'clubs': summary}, fixtures())

    def test_manual_coverage_and_verdict_do_not_count_all_catalogue_as_success(self):
        core = archive.with_catalogs({'clubs': method()}, 10)
        entry = dict(engine='mixed_clubs', fixtures=fixtures(), comparison_methods=core)
        info = manual.coverage(entry)
        self.assertEqual(info['catalog_total'], 59049)
        self.assertEqual(info['catalog_methods'], ['clubs'])
        self.assertTrue(info['catalog_complete'])
        self.assertEqual(info['proposal_count'], 3)
        fourth = method(count=4)['ranking'][3]['sequence']
        verdict = manual.evaluate(entry, fourth)
        self.assertEqual(verdict['state'], 'miss')
        self.assertFalse(verdict['actual_in_choices'])
        self.assertGreater(models.probability_for_sequence(core['clubs'], fourth), 0)
        self.assertEqual(len(verdict['rows']), 3)

    def test_twenty_match_archive_stays_compact_and_input_unchanged(self):
        core = {'clubs': method(20)}
        before = deepcopy(core)
        saved = proposals.capture(core, fixtures(20), now=NOW, artifact={})
        self.assertEqual(saved['methods']['clubs']['scenario_catalog']['total'], 3**20)
        self.assertLess(len(json.dumps(saved)), 6000)
        self.assertEqual(core, before)
        proposals.validate(saved, core, fixtures(20))


if __name__ == '__main__':
    unittest.main()
