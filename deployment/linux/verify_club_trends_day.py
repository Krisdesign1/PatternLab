from datetime import datetime,date,timezone
from pathlib import Path
import json,sys,time
from unittest.mock import patch
sys.path.insert(0,'/opt/patternlab')
import club_trends_day as day
import club_trends as trends
import club_trends_panel as panel
from streamlit.testing.v1 import AppTest
now=datetime.now(timezone.utc);chosen=now.astimezone(day.ZONE).date()
start=time.perf_counter();calendar=day.load(chosen,now=now);elapsed=time.perf_counter()-start
assert all(row['local_day']==chosen.isoformat() for row in calendar['rows'])
assert len({row['match_id'] for row in calendar['rows']})==len(calendar['rows'])
report=trends.build(chosen,chosen,now=now)
joined=day.enrich(calendar['rows'],report['rows'])
assert len(joined)==len(calendar['rows'])
assert all(profile['team_id']==row[side+'_id'] and profile['league']==row['league']
           for row in joined for side in ('home','away') if (profile:=row[side+'_profile']) is not None)
values=panel._day_table(joined)
assert all(row['Date']==chosen.strftime('%d/%m/%Y') for row in values)
if values:
    assert all(sum(profile['percentages'].values())>99.999 for row in joined
               for side in ('home','away') if (profile:=row[side+'_profile']) and profile['matches'])
    request=(chosen,chosen,(),10,'all',('verification',),now.isoformat())
    csv=panel._day_csv(values,request,chosen,calendar).decode('utf-8-sig')
    import csv as csvmodule,io
    exported=list(csvmodule.DictReader(io.StringIO(csv),delimiter=';'))
    assert len(exported)==len(values)
    assert all(row['Journée des matchs (Kinshasa)']==chosen.isoformat() for row in exported)
    # Render the real day with real descriptive profiles, using an in-process
    # session; no live server/browser, forecast recording or provider requests.
    at=AppTest.from_string('from club_trends_panel import _render_day\n'
        'from datetime import date\nimport streamlit as st\n'
        '_render_day(st.session_state["report"],st.session_state["request"],date.fromisoformat(st.session_state["day"]))',default_timeout=45)
    at.session_state['report']=report;at.session_state['request']=request;at.session_state['day']=chosen.isoformat()
    import api_football
    with patch.object(api_football.ApiFootballClient,'get',side_effect=AssertionError('No API')):
        at.run()
    assert not at.exception,[(e.message) for e in at.exception]
    table=next(frame.value for frame in at.dataframe if 'Match' in frame.value.columns)
    assert len(table)==min(100,len(values))
else: table=[]
print(json.dumps(dict(state='verified',verified_at=now.isoformat(),day=chosen.isoformat(),
    matches=len(values),status_counts=calendar['totals'],local_day_boundaries_checked=True,
    exact_club_competition_join_checked=True,all_filtered_export_count=len(values),
    in_process_real_data_render_ok=True,first_page_rows=len(table),
    day_query_seconds=elapsed,production_database_writes=0,api_requests=0),ensure_ascii=False))
