"""Authorization fails closed; web controls cannot bypass server-side checks."""
import copy
import time
import unittest
from unittest.mock import MagicMock, patch

from streamlit.testing.v1 import AppTest

import admin_access as access
import api_football_panel as api_panel
import automation_panel as automation


AUTH = dict(redirect_uri='https://example.com/oauth2callback', cookie_secret='test-only',
            client_id='client', client_secret='secret', server_metadata_url='https://issuer/.well-known/openid-configuration')
CLAIMS = dict(is_logged_in=True, iss='https://issuer', sub='owner', exp=2000,
              email='owner@example.com', email_verified=True)


class AdminPolicyTests(unittest.TestCase):
    def config(self):
        return dict(auth=copy.deepcopy(AUTH), access=dict(admin_emails=['owner@example.com']))

    def allowed(self, config=None, claims=None, **kwargs):
        return access.authorized(self.config() if config is None else config,
                                 CLAIMS if claims is None else claims, now=1000, **kwargs)

    def test_default_configuration_has_no_administrator_even_with_claimed_email(self):
        for config in ({}, {'access': {}}, {'access': {'admin_emails': ['owner@example.com']}}):
            self.assertFalse(self.allowed(config=config))

    def test_verified_explicit_email_is_required(self):
        self.assertTrue(self.allowed())
        for changes in ({'is_logged_in': False}, {'email_verified': False}, {'email_verified': 'true'},
                        {'email': 'other@example.com'}, {'iss': ''}, {'sub': ''}):
            with self.subTest(changes=changes):
                self.assertFalse(self.allowed(claims=dict(CLAIMS, **changes)))

    def test_email_case_is_normalized_but_no_domain_or_substring_match(self):
        self.assertTrue(self.allowed(claims=dict(CLAIMS, email='OWNER@EXAMPLE.COM')))
        self.assertFalse(self.allowed(claims=dict(CLAIMS, email='prefix-owner@example.com')))
        config = self.config(); config['access']['admin_emails'] = '@example.com'
        self.assertFalse(self.allowed(config=config))

    def test_immutable_identity_requires_both_issuer_and_subject(self):
        config = self.config(); config['access'] = dict(admin_identities=[dict(issuer='https://issuer', subject='owner')])
        self.assertTrue(self.allowed(config=config, claims=dict(CLAIMS, email_verified=False)))
        self.assertFalse(self.allowed(config=config, claims=dict(CLAIMS, iss='https://other')))
        self.assertFalse(self.allowed(config=config, claims=dict(CLAIMS, sub='another')))

    def test_expired_missing_malformed_and_nonfinite_expiration_are_refused(self):
        for expiration in (999, 1000, None, '2000', True, float('inf'), float('nan')):
            with self.subTest(expiration=expiration):
                self.assertFalse(self.allowed(claims=dict(CLAIMS, exp=expiration)))

    def test_private_local_flag_requires_literal_loopback_bind(self):
        config = dict(access=dict(local_owner=True))
        for address in ('127.0.0.1', '::1'):
            self.assertTrue(self.allowed(config=config, claims={}, server_address=address))
        for address in ('', None, '0.0.0.0', '::', 'localhost', '127.0.0.1.evil.example', '192.168.1.2'):
            self.assertFalse(self.allowed(config=config, claims={}, server_address=address))
        for value in (False, 'true', 1):
            self.assertFalse(self.allowed(config={'access': {'local_owner': value}}, server_address='127.0.0.1'))

    def test_browser_role_claim_cannot_grant_access(self):
        self.assertFalse(self.allowed(config={}, claims=dict(CLAIMS, admin=True, role='admin')))
        self.assertFalse(self.allowed(claims={'admin': True, 'is_logged_in': True}))

    def test_named_provider_must_be_complete_and_explicit(self):
        config = self.config()
        config['auth'] = dict(redirect_uri=AUTH['redirect_uri'], cookie_secret=AUTH['cookie_secret'],
                              google={key: AUTH[key] for key in ('client_id', 'client_secret', 'server_metadata_url')})
        self.assertFalse(self.allowed(config=config))
        config['access']['provider'] = 'google'
        self.assertTrue(self.allowed(config=config))
        del config['auth']['google']['client_secret']
        self.assertFalse(self.allowed(config=config))

    def test_runtime_role_is_recomputed_and_not_copied_from_session_state(self):
        config = self.config()
        with patch.object(access, '_secrets', return_value=config), patch.object(access, '_claims', return_value=CLAIMS), \
                patch.object(access.st, 'get_option', return_value='0.0.0.0'), patch.object(access, 'time', return_value=1000):
            self.assertTrue(access.can_admin())
            config['access']['admin_emails'] = []
            self.assertFalse(access.can_admin())


class AdminBoundaryTests(unittest.TestCase):
    def test_read_only_api_panel_disables_controls_without_network(self):
        client = MagicMock()
        with patch.object(access, 'can_admin', return_value=False), \
                patch.object(api_panel, 'load_key', return_value='test-only-key'), \
                patch.object(api_panel, 'ApiFootballClient', return_value=client), \
                patch.object(api_panel, '_check_report', return_value={}), \
                patch.object(api_panel, 'load_fixtures', return_value=[]):
            at = AppTest.from_string('from api_football_panel import render_api_football\nrender_api_football()').run()
            self.assertFalse(at.exception)
            self.assertTrue(at.button(key='api_check').disabled)
            self.assertTrue(next(button for button in at.button if button.label == 'Rechercher les compétitions').disabled)
            client.status.assert_not_called(); client.leagues.assert_not_called(); client.fixtures.assert_not_called()

    def test_forged_api_button_event_is_stopped_before_using_server_key(self):
        client = MagicMock()
        with patch.object(access, 'can_admin', return_value=False), \
                patch.object(api_panel, 'load_key', return_value='test-only-key'), \
                patch.object(api_panel, 'ApiFootballClient', return_value=client), \
                patch.object(api_panel, '_check_report', return_value={}), \
                patch.object(api_panel, '_save_check') as save, patch.object(api_panel.st, 'button', return_value=True):
            at = AppTest.from_string('from api_football_panel import render_api_football\nrender_api_football()').run()
            self.assertFalse(at.exception)
            self.assertTrue(any('nécessite un compte administrateur' in value.value for value in at.warning))
            client.status.assert_not_called(); save.assert_not_called()

    def test_forged_configuration_submit_does_not_save_shared_configuration(self):
        with patch.object(access, 'can_admin', return_value=False), \
                patch.object(automation.worker, 'save_config') as save, \
                patch.object(automation.st, 'form_submit_button', return_value=True):
            script = 'import automation_panel as panel\npanel._configuration(dict(panel.worker.DEFAULT_CONFIG))'
            at = AppTest.from_string(script).run()
            self.assertFalse(at.exception)
            self.assertTrue(at.warning)
            save.assert_not_called()

    def test_role_revoked_between_render_and_execution_stops_action(self):
        with patch.object(access, 'can_admin', side_effect=[True, False]), \
                patch.object(api_panel, 'load_key', return_value='test-only-key'), \
                patch.object(api_panel, 'ApiFootballClient') as factory, \
                patch.object(api_panel, '_check_report', return_value={}), \
                patch.object(api_panel.st, 'button', return_value=True), patch.object(access, 'read_only_notice'):
            at = AppTest.from_string('from api_football_panel import render_api_football\nrender_api_football()').run()
            self.assertFalse(at.exception)
            self.assertTrue(at.warning)
            factory.return_value.status.assert_not_called()


if __name__ == '__main__':
    unittest.main()
