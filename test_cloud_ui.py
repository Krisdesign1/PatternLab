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
        st.success('Page ready')
    render()


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

    def test_unconfigured_status_clearly_reports_local_only(self):
        with patch.object(cloud_storage, 'configured', return_value=False), \
                patch.object(cloud_storage, 'status', side_effect=AssertionError('No remote network')):
            app = AppTest.from_function(status_app).run()
        self.assertFalse(app.exception)
        self.assertIn('local uniquement', app.warning[0].value)


if __name__ == '__main__':
    unittest.main()
