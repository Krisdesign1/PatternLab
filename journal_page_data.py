"""Small revision-keyed journal projections for browsing, never forecasts.

Full distributions are verified when projected, then discarded from the cache.
Opening a fiche still reads and verifies that record afresh. Database/WAL and
fixture-store revisions invalidate these display-only caches after any writer.
"""
from datetime import date, datetime

import streamlit as st

import api_football as api
import journal_browser_data
import journal_tracking
import manual_analysis_store as manual
import manual_journal_rows
import prediction_journal as journal
import result_explanations


def revision():
    return (journal_tracking._file_revision(journal.DB_PATH),
            journal_tracking._file_revision(str(journal.DB_PATH) + '-wal'),
            journal_tracking._file_revision(manual.database_path()),
            journal_tracking._file_revision(str(manual.database_path()) + '-wal'),
            api.data_signature(api.STORE_PATH))


@st.cache_data(ttl=60, max_entries=4, show_spinner=False)
def inventory(storage_revision):
    """Counts and dates only; no methods or result history are retained."""
    return journal.overview(include_mixed=False)


@st.cache_data(ttl=60, max_entries=4, show_spinner=False)
def day_rows(day, storage_revision, minute, *, _now):
    """Cache small UI rows, with a minute boundary for time-based statuses."""
    entries = journal.list_entries(day=day, include_mixed=False)
    annotated = result_explanations.annotate_entries(entries, now=_now)
    confirmed = journal_browser_data.build_rows(annotated, now=_now, day=day)
    manual_entries, saved_rows, warning = [], [], False
    try:
        manual_entries = manual.list_entries(day=day.isoformat())
        if manual_entries:
            ids = sorted({fixture['match_id'] for entry in manual_entries
                          for fixture in entry['fixtures']}, key=int)
            raw = api.load_fixtures(ids=ids)
            saved_rows = manual_journal_rows.build_rows(manual_entries, raw, now=_now)
    except (manual.ManualAnalysisError, api.ApiFootballError, OSError, ValueError):
        warning = True
        if manual_entries:
            saved_rows = manual_journal_rows.build_rows(manual_entries, None, now=_now)
    # These are not used by the browser; a selected fiche reloads its exact
    # immutable record and evaluates current local observations independently.
    for row in saved_rows:
        row.pop('forecast', None)
        row.pop('evaluation', None)
    rows = sorted(confirmed + saved_rows, key=lambda row: (row['created_at'], row['id']), reverse=True)
    return dict(rows=rows, confirmed_count=len(entries), manual_count=len(manual_entries),
                manual_warning=warning)


def load_rows(day, *, now):
    if not isinstance(day, date) or isinstance(day, datetime):
        raise ValueError('La journée consultée doit être une date locale.')
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError('L’heure de consultation doit préciser son fuseau.')
    return day_rows(day, revision(), int(now.timestamp()) // 60, _now=now)
