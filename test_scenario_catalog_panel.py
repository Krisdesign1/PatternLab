"""Complete-catalogue UI: pagination is presentation, never model selection."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import pandas as pd
import streamlit as st
from streamlit.testing.v1 import AppTest

import prospective_models as models
import scenario_catalog_panel as panel


def method(size=10, version='clubs-ui-fixture'):
    positions = [{'1st': .2, 'Égal': .3, '2nd': .5} for _ in range(size)]
    return models._method(version,
        models._distribution('independent', size, distributions=positions), {})


def descriptor(value):
    size = value['distribution']['size']
    return dict(version='catalogue-ui-test', size=size, total=3**size,
                distribution_sha256=value['version'], sha256=value['version'])


def page(value, offset=0, page_size=50):
    size = value['distribution']['size']
    total = 3**size
    rows = []
    for index in range(offset, min(total, offset + page_size)):
        cursor, sequence = index, []
        for _ in range(size):
            sequence.append(panel.VALUES[cursor % 3]); cursor //= 3
        rows.append(dict(sequence=list(reversed(sequence)), probability=1/total))
    return dict(ranking=rows, total=total, has_next=offset + len(rows) < total,
                offset=offset, page_size=page_size)


class ScenarioCataloguePanelTests(unittest.TestCase):
    def setUp(self):
        self.methods = {'clubs': method(), 'combined': method(version='combined-ui-fixture')}
        self.before = deepcopy(self.methods)
        self._patch(panel.catalog, 'descriptor', side_effect=descriptor)
        self.validation = self._patch(panel.catalog, 'validate_descriptor')
        self.pages = self._patch(panel, '_page', side_effect=page)
        self.lookup = self._patch(panel.catalog, 'lookup', side_effect=lambda value, sequence:
            dict(sequence=list(sequence), probability=.012345, rank=None,
                 total=3**value['distribution']['size']))
        self._patch(panel.tables, 'render_sequence', side_effect=lambda sequence, **kwargs:
            st.caption((kwargs.get('label') or '') + ' : ' + ' → '.join(sequence)))
        self.grid = self._patch(panel.table_views, 'table', side_effect=lambda rows, **kwargs:
            st.dataframe(pd.DataFrame(rows), hide_index=True))
        self.native_grid = self._patch(panel.table_views, 'dataframe', side_effect=lambda rows, **kwargs:
            st.dataframe(pd.DataFrame(rows), hide_index=True, column_config=kwargs.get('column_config')))
        self.network = self._patch(models, 'build_methods',
            side_effect=AssertionError('Catalogue browsing must not train or load a current model'))

    def _patch(self, obj, name, *args, **kwargs):
        task = patch.object(obj, name, *args, **kwargs)
        result = task.start(); self.addCleanup(task.stop)
        return result

    def app(self, methods=None, actual=None, size=10):
        value = self.methods if methods is None else methods
        source = ('import scenario_catalog_panel as p\n'
                  f'p.render({value!r}, actual={actual!r}, key="catalogue-test")')
        self.at = AppTest.from_string(source, default_timeout=15).run()
        self.assertFalse(self.at.exception)
        return self.at

    def table(self):
        return self.at.dataframe[0].value

    def rows_selector(self):
        return next(item for item in self.at.selectbox if item.label == 'Scénarios à afficher')

    def test_ten_matches_immediately_show_fifty_with_complete_count_and_no_mutation(self):
        at = self.app()
        self.assertEqual(len(self.table()), 50)
        self.assertEqual(self.table()['Rang'].tolist(), list(range(1, 51)))
        self.assertEqual(at.metric[0].value, '59 049')
        self.assertEqual(at.number_input[0].max, 1181)
        self.assertEqual(self.pages.call_args.args[1:], (0, 50))
        self.assertEqual(self.methods, self.before)
        self.network.assert_not_called()
        self.assertTrue(any('catalogue complet est lié' in item.value for item in at.caption))

    def test_old_distribution_is_consultable_without_inventing_historical_proposals(self):
        methods = deepcopy(self.methods)
        for value in methods.values():
            value.pop('scenario_catalog', None)
        at = self.app(methods=methods)
        self.assertEqual(len(self.table()), 50)
        self.assertTrue(any('pas des propositions enregistrées' in item.value for item in at.caption))

    def test_next_and_previous_pages_do_not_retain_only_first_three(self):
        at = self.app()
        next_button = next(item for item in at.button if item.label == 'Page suivante')
        next_button.click().run()
        self.assertFalse(at.exception)
        self.assertEqual(self.table()['Rang'].tolist(), list(range(51, 101)))
        previous = next(item for item in at.button if item.label == 'Page précédente')
        previous.click().run()
        self.assertFalse(at.exception)
        self.assertEqual(self.table()['Rang'].tolist(), list(range(1, 51)))

    def test_last_page_remains_accessible_and_stops_at_total(self):
        at = self.app()
        at.number_input[0].set_value(1181).run()
        self.assertFalse(at.exception)
        self.assertEqual(self.table()['Rang'].tolist(), list(range(59001, 59050)))
        self.assertTrue(next(item for item in at.button if item.label == 'Page suivante').disabled)

    def test_method_change_has_its_own_page_position(self):
        at = self.app()
        at.number_input[0].set_value(2).run()
        at.selectbox[0].select('combined').run()
        self.assertFalse(at.exception)
        self.assertEqual(self.table()['Rang'].iloc[0], 1)
        at.selectbox[0].select('clubs').run()
        self.assertFalse(at.exception)
        self.assertEqual(self.table()['Rang'].iloc[0], 51)

    def test_page_size_options_are_real_visible_row_counts(self):
        at = self.app()
        for amount in (100, 250, 500, 50):
            with self.subTest(rows=amount):
                self.rows_selector().select(amount).run()
                self.assertFalse(at.exception)
                self.assertEqual(len(self.table()), amount)
                self.assertEqual(self.pages.call_args.args[1:], (0, amount))
        self.assertEqual(self.methods, self.before)

    def test_all_displays_every_ten_match_outcome_in_a_virtualized_grid(self):
        at = self.app()
        self.rows_selector().select('all').run()
        self.assertFalse(at.exception)
        self.assertEqual(len(self.table()), 59049)
        self.assertEqual(self.table()['Rang'].iloc[-1], 59049)
        self.assertEqual(self.pages.call_args.args[1:], (0, 59049))
        self.native_grid.assert_called_once()
        self.assertEqual(self.native_grid.call_args.kwargs['height'], 650)
        self.assertEqual(list(self.table()['Scénario complet'].iloc[0]),
                         [f'{index} 1st' for index in range(1, 11)])
        self.assertTrue(at.number_input[0].disabled)
        self.assertEqual(self.methods, self.before)

    def test_size_change_resets_page_and_method_keeps_its_size_and_page(self):
        at = self.app()
        at.number_input[0].set_value(4).run()
        self.rows_selector().select(100).run()
        self.assertEqual(at.number_input[0].value, 1)
        at.number_input[0].set_value(3).run()
        at.selectbox[0].select('combined').run()
        self.assertEqual(self.rows_selector().value, 50)
        self.rows_selector().select(250).run()
        at.selectbox[0].select('clubs').run()
        self.assertFalse(at.exception)
        self.assertEqual(self.rows_selector().value, 100)
        self.assertEqual(at.number_input[0].value, 3)
        self.assertEqual(self.table()['Rang'].iloc[0], 201)
        at.run()
        self.assertEqual(self.rows_selector().value, 100)
        self.assertEqual(at.number_input[0].value, 3)

    def test_all_for_smaller_groups_has_exact_count_and_observed_badge(self):
        methods={'clubs':method(size=7)}
        at=self.app(methods=methods,actual=['1st']*7)
        self.rows_selector().select('all').run()
        self.assertFalse(at.exception)
        self.assertEqual(len(self.table()),2187)
        self.assertEqual(self.table()['Rang'].iloc[-1],2187)
        self.assertEqual(list(self.table()['Résultat final'].iloc[0]),['Scénario observé'])
        self.assertEqual(self.pages.call_args.args[1:],(0,2187))

    def test_hide_and_return_restores_method_page_and_size(self):
        source = ('import streamlit as st\nimport scenario_catalog_panel as p\n'
                  'shown=st.toggle("Afficher le catalogue",value=True,key="show-catalog")\n'
                  f'if shown:p.render({self.methods!r}, key="catalogue-test")')
        self.at=at=AppTest.from_string(source,default_timeout=15).run()
        at.selectbox[0].select('combined').run()
        self.rows_selector().select(250).run()
        at.number_input[0].set_value(3).run()
        at.toggle[0].set_value(False).run()
        self.assertFalse(at.dataframe)
        at.toggle[0].set_value(True).run()
        self.assertFalse(at.exception)
        self.assertEqual(at.selectbox[0].value, 'combined')
        self.assertEqual(self.rows_selector().value, 250)
        self.assertEqual(at.number_input[0].value, 3)
        self.assertEqual(self.table()['Rang'].iloc[0], 501)

    def test_exact_sequence_lookup_works_beyond_reference_without_a_catalogue_scan(self):
        at = self.app()
        at.text_input[0].set_value('egal ' * 10).run()
        next(item for item in at.button if item.label == 'Consulter sa probabilité').click().run()
        self.assertFalse(at.exception)
        self.lookup.assert_called_once()
        self.assertEqual(self.lookup.call_args.args[1], ['Égal'] * 10)
        self.assertTrue(any(item.label == 'Probabilité conservée de ce scénario' for item in at.metric))
        self.assertEqual(len(self.table()), 50)

    def test_invalid_lookup_does_not_score_a_prefix(self):
        at = self.app()
        at.text_input[0].set_value('2nd 1st').run()
        next(item for item in at.button if item.label == 'Consulter sa probabilité').click().run()
        self.assertFalse(at.exception)
        self.lookup.assert_not_called()
        self.assertTrue(any('exactement 10 résultats' in item.value for item in at.warning))

    def test_observed_sequence_is_not_labeled_a_catalogue_selection_success(self):
        at = self.app(actual=['Égal'] * 10)
        self.assertFalse(at.exception)
        self.assertTrue(any('Scénario observé' in item.value for item in at.caption))
        self.assertTrue(any('ne mesure pas la réussite' in item.value for item in at.caption))
        self.assertTrue(all(value == '' for value in self.table()['Résultat final']))
        self.assertFalse(at.success)

    def test_ranking_only_legacy_payload_does_not_invent_a_full_catalogue(self):
        old = {'clubs': {'version':'old', 'ranking':self.methods['clubs']['ranking']}}
        at = self.app(methods=old)
        self.pages.assert_not_called(); self.lookup.assert_not_called()
        self.assertFalse(at.dataframe)
        self.assertTrue(any('ne conserve pas de distribution complète' in item.value for item in at.caption))

    def test_page_interruption_is_explicit_and_keeps_search_and_compact_export(self):
        self.pages.side_effect = TimeoutError('Interruption contrôlée du calcul de la page.')
        at = self.app()
        self.assertFalse(at.exception)
        self.assertFalse(at.dataframe)
        self.assertTrue(any('Page non chargée' in item.value for item in at.warning))
        self.assertEqual(len(at.text_input), 1)
        self.assertTrue(any('catalogue complet' in item.proto.label for item in at.get('download_button')))

    def test_all_interruption_does_not_silently_show_first_fifty(self):
        def bounded(value, offset, page_size):
            if page_size > 1000:
                raise TimeoutError('Chargement complet interrompu ; aucun préfixe complet annoncé.')
            return page(value, offset, page_size)
        self.pages.side_effect=bounded
        at=self.app()
        self.rows_selector().select('all').run()
        self.assertFalse(at.exception)
        self.assertFalse(at.dataframe)
        self.assertEqual(self.rows_selector().value, 'all')
        self.assertTrue(any('Page non chargée' in item.value for item in at.warning))
        self.assertTrue(any('catalogue complet' in item.proto.label for item in at.get('download_button')))
        self.rows_selector().select(500).run()
        self.assertEqual(len(self.table()),500)

    def test_compact_export_and_csv_preserve_complete_distribution_and_absolute_probabilities(self):
        value = self.methods['clubs']
        original = deepcopy(value)
        exported = json.loads(panel._compact(value, descriptor(value), [{'match_id':'1','home':'A','away':'B'}]))
        self.assertEqual(exported['catalogue']['total'], 59049)
        self.assertEqual(exported['method']['distribution'], value['distribution'])
        self.assertEqual(exported['ordered_fixtures'][0]['match_id'], '1')
        self.assertEqual(value, original)
        csv = panel._csv([dict(sequence=['1st', '2nd'], probability=.002)], 8, None).decode('utf-8-sig')
        self.assertIn('9,1st → 2nd,0.2,', csv)


class CompleteCatalogueLoadingTests(unittest.TestCase):
    def setUp(self):
        panel._page.clear()
        self.addCleanup(panel._page.clear)

    def test_real_all_reader_returns_all_59049_without_mutating_forecast(self):
        value=method()
        original=deepcopy(value)
        complete=panel._page(value,0,59049)
        self.assertEqual(len(complete['ranking']),59049)
        self.assertEqual(complete['total'],59049)
        self.assertFalse(complete['has_next'])
        self.assertEqual(complete['ranking'][:3],value['ranking'])
        self.assertAlmostEqual(sum(row['probability'] for row in complete['ranking']),1,places=10)
        self.assertEqual(value,original)

    def test_small_all_reads_every_outcome_with_existing_paged_reader(self):
        value=method(size=3)
        complete=panel._page(value,0,27)
        self.assertEqual(len(complete['ranking']),27)

    def test_astronomical_all_fails_before_allocation_and_preserves_compact_export(self):
        value=method(size=20)
        with patch.object(panel.catalog,'_all',side_effect=AssertionError('No giant enumeration')):
            with self.assertRaisesRegex(ValueError,'budget mémoire'):
                panel._page(value,0,3**20)
        self.assertEqual(json.loads(panel._compact(value,panel.catalog.descriptor(value),None))['catalogue']['total'],3**20)
        self.assertEqual(len(panel._page(value,0,500)['ranking']),500)

    def test_unchanged_page_uses_cache_instead_of_reranking(self):
        value=method()
        with patch.object(panel.catalog,'page',wraps=panel.catalog.page) as reader:
            panel._page(value,0,50)
            panel._page(deepcopy(value),0,50)
            self.assertEqual(reader.call_count,1)
            panel._page(value,50,50)
            self.assertEqual(reader.call_count,2)


if __name__ == '__main__':
    unittest.main()
