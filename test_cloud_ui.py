"""Cloud failures suspend a page before its controls can mutate data."""
from contextlib import contextmanager
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import cloud_storage
import cloud_ui


def sample_app():
    import streamlit as st
    from cloud_ui import guarded
    @guarded
    def render():
        st.session_state['page_executions'] = st.session_state.get('page_executions', 0) + 1
        st.success('Page ready')
    render()


def busy_notice_app():
    from cloud_ui import _busy_notice
    _busy_notice(100, 'notice_retry')


def status_app():
    from cloud_ui import render_storage_status
    render_storage_status()


class CloudUiTests(unittest.TestCase):
    def test_required_storage_failure_does_not_execute_page(self):
        @contextmanager
        def failed():
            raise cloud_storage.CloudStorageError('Connexion absente.')
            yield
        with patch.object(cloud_storage, 'transaction', failed):
            app = AppTest.from_function(sample_app).run()
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        self.assertFalse(app.success)
        self.assertFalse(app.button)
        self.assertNotIn('page_executions', app.session_state)

    def test_busy_page_shows_retry_without_exception(self):
        @contextmanager
        def busy():
            raise cloud_storage.CloudStorageBusy('Busy')
            yield
        with patch.object(cloud_storage, 'transaction', busy):
            app = AppTest.from_function(sample_app).run()
        self.assertFalse(app.exception)
        self.assertTrue(app.info)
        self.assertFalse(app.success)
        self.assertEqual([button.label for button in app.button], ['Réessayer maintenant'])
        self.assertNotIn('page_executions', app.session_state)

    def test_retry_renders_page_when_shared_update_has_finished(self):
        attempts = []

        @contextmanager
        def first_busy_then_ready():
            attempts.append(None)
            if len(attempts) == 1:
                raise cloud_storage.CloudStorageBusy('Busy')
            yield

        with patch.object(cloud_storage, 'transaction', first_busy_then_ready):
            app = AppTest.from_function(sample_app).run()
            self.assertFalse(app.success)
            self.assertNotIn('page_executions', app.session_state)
            app.button[0].click().run()

        self.assertFalse(app.exception)
        self.assertEqual([message.value for message in app.success], ['Page ready'])
        self.assertEqual(app.session_state['page_executions'], 1)
        self.assertEqual(len(attempts), 2)
        self.assertFalse(app.button)
        self.assertFalse(app.info)

    def test_notice_waits_until_deadline_without_accessing_shared_data(self):
        with patch.object(cloud_ui, 'monotonic', return_value=99), \
                patch.object(cloud_ui.st, 'rerun') as rerun, \
                patch.object(cloud_storage, 'transaction', side_effect=AssertionError('No storage polling')), \
                patch.object(cloud_storage, 'status', side_effect=AssertionError('No database polling')), \
                patch('api_football.ApiFootballClient', side_effect=AssertionError('No API requests')):
            app = AppTest.from_function(busy_notice_app).run()

        self.assertFalse(app.exception)
        self.assertTrue(app.info)
        self.assertEqual([button.label for button in app.button], ['Réessayer maintenant'])
        self.assertTrue(any('10 secondes' in caption.value for caption in app.caption))
        rerun.assert_not_called()

    def test_notice_requests_one_full_app_rerun_at_deadline(self):
        with patch.object(cloud_ui, 'monotonic', return_value=100), \
                patch.object(cloud_ui.st, 'rerun') as rerun, \
                patch.object(cloud_storage, 'transaction', side_effect=AssertionError('No storage polling')), \
                patch.object(cloud_storage, 'status', side_effect=AssertionError('No database polling')), \
                patch('api_football.ApiFootballClient', side_effect=AssertionError('No API requests')):
            app = AppTest.from_function(busy_notice_app).run()

        self.assertFalse(app.exception)
        rerun.assert_called_once_with(scope='app')

    def test_notice_manual_retry_does_not_wait_for_deadline(self):
        with patch.object(cloud_ui, 'monotonic', return_value=99), \
                patch.object(cloud_ui.st, 'rerun') as rerun:
            app = AppTest.from_function(busy_notice_app).run()
            rerun.assert_not_called()
            app.button[0].click().run()

        self.assertFalse(app.exception)
        rerun.assert_called_once_with(scope='app')

    def test_manual_retry_while_still_busy_finishes_without_hot_loop(self):
        attempts = []

        @contextmanager
        def still_busy():
            attempts.append(None)
            raise cloud_storage.CloudStorageBusy('Busy')
            yield

        with patch.object(cloud_storage, 'transaction', still_busy), \
                patch.object(cloud_ui, 'monotonic', return_value=100):
            app = AppTest.from_function(sample_app).run(timeout=3)
            self.assertEqual(len(attempts), 1)
            app.button[0].click().run(timeout=3)

        self.assertFalse(app.exception)
        # AppTest starts a full run for the click. The retry button then requests
        # one more full run, where its trigger has reset and the new timer waits.
        self.assertEqual(len(attempts), 3)
        self.assertTrue(app.info)
        self.assertEqual([button.label for button in app.button], ['Réessayer maintenant'])
        self.assertFalse(app.success)
        self.assertNotIn('page_executions', app.session_state)

    def test_each_failed_attempt_creates_a_fresh_future_retry_deadline(self):
        executions = []

        @contextmanager
        def busy():
            raise cloud_storage.CloudStorageBusy('Busy')
            yield

        @cloud_ui.guarded
        def render():
            executions.append(None)

        with patch.object(cloud_storage, 'transaction', busy), \
                patch.object(cloud_ui, 'monotonic', side_effect=[100, 200]), \
                patch.object(cloud_ui, '_busy_notice') as notice:
            render()
            render()

        self.assertEqual(executions, [])
        self.assertEqual([call.args[0] for call in notice.call_args_list], [110, 210])
        self.assertEqual(notice.call_args_list[0].args[1], notice.call_args_list[1].args[1])

    def test_unconfigured_status_clearly_reports_local_only(self):
        with patch.object(cloud_storage, 'configured', return_value=False), \
                patch.object(cloud_storage, 'status', side_effect=AssertionError('No remote network')):
            app = AppTest.from_function(status_app).run()
        self.assertFalse(app.exception)
        self.assertIn('local uniquement', app.warning[0].value)


if __name__ == '__main__':
    unittest.main()
