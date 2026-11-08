"""Local scheduler adapter for the enrolled causal candidate confirmation.

Registration is the explicit activation. This adapter owns no provider client,
model fit or schedule selection: the core applies its frozen UTC capture window
and the existing automation gate supplies fresh quota-aware fixture responses.
"""
from pathlib import Path

import api_football as api
import cloud_storage


VERSION = 'causal-confirmation-worker-v1'


def _root(root=None):
    return Path(api.DATA_DIR if root is None else root).resolve()


def capture_cycle(root=None, *, clock=None, refresh=None, check_time=None):
    """Pass the scheduler's callbacks unchanged; pauses remain scheduler-owned."""
    if cloud_storage.configured():
        return dict(status='disabled', created=0, reason='Le suivi causal exige le stockage local.')
    import causal_confirmation as confirmation
    return confirmation.capture_due(root=_root(root), clock=clock, refresh=refresh, check_time=check_time)


def reconcile(root=None, *, now=None):
    """Reconcile saved forecasts even outside the capture window or API quota."""
    root = _root(root)
    if now is not None and root == Path(api.DATA_DIR).resolve():
        raise ValueError('Le suivi réel utilise uniquement son horloge actuelle.')
    if cloud_storage.configured():
        return dict(status='disabled', reason='Le suivi causal exige le stockage local.')
    import causal_confirmation as confirmation
    return confirmation.reconcile(root=root, now=now)


def refresh_entries(root=None):
    """Read-only adapter for the existing scheduler's fair result-ID queue."""
    if cloud_storage.configured():
        return []
    import causal_confirmation as confirmation
    return confirmation.refresh_entries(root=_root(root))


def status(root=None):
    """Read saved evidence only; never enroll, capture, fit or call the API."""
    if cloud_storage.configured():
        return dict(status='disabled', reason='Le suivi causal exige le stockage local.')
    import causal_confirmation as confirmation
    return confirmation.report(root=_root(root))
