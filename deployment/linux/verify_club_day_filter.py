"""Check the club-list day filter against real IDs, without API or database writes."""
from datetime import datetime,date,timezone
from pathlib import Path
from io import StringIO
import csv,json,sys
from unittest.mock import patch
sys.path.insert(0,'/opt/patternlab')
import api_football as api
import club_trends as trends
import club_trends_day as day
import club_trends_panel as panel
from streamlit.testing.v1 import AppTest
now=datetime.now(timezone.utc);selected=now.astimezone(day.ZONE).date()
calendar=day.load(selected,now=now)
# A bounded one-month profile window suffices to verify membership/display.
start=date(selected.year,selected.month,1)
report=trends.build(start,selected,now=now)
original_count=len(report['rows'])
expected=day.filter_profiles(report['rows'],calendar['rows'])
request=(start,selected,(),10,'all',api.data_signature(api.STORE_PATH),now.isoformat())
source=('from club_trends_panel import render\nrender()')
app=AppTest.from_string(source,default_timeout=40)
app.session_state['analysis_selected_day']=selected
app.session_state['analysis_follow_today']=False
app.session_state['club_trends_selected_day']=selected
app.session_state['club_trends_start']=start
app.session_state['club_trends_end']=selected
app.session_state['club_trends_request']=request
with patch.object(api.ApiFootballClient,'get',side_effect=AssertionError('No API')), \
     patch.object(panel,'_summary',return_value=report), \
     patch.object(panel,'_day_calendar',return_value=calendar), \
     patch.object(panel.st,'download_button',wraps=panel.st.download_button) as downloads:
    app.run();assert not app.exception
    assert app.toggle(key='club_trends_day_only').value is False
    app.toggle(key='club_trends_day_only').set_value(True).run();assert not app.exception
    if expected['rows']:
        table=next(x.value for x in app.dataframe if 'Tendance dominante' in x.value.columns)
        keys={(r['ID club API'],r['ID compétition API']) for _,r in table.iterrows()}
        allowed={(r['team_id'],r['league']) for r in expected['rows']}
        assert keys<=allowed
        args=next(c for c in reversed(downloads.call_args_list) if c.args[0]=='Exporter tous les profils filtrés (CSV)').kwargs
        exported=list(csv.DictReader(StringIO(args['data'].decode('utf-8-sig')),delimiter=';'))
        assert len(exported)==len(expected['rows'])
        assert all(r['Filtre matchs · journée de Kinshasa']==selected.isoformat() for r in exported)
    else:
        exported=[]
        assert any('Aucun' in x.value for x in app.info)
    app.toggle(key='club_trends_day_only').set_value(False).run();assert not app.exception
assert len(report['rows'])==original_count
print(json.dumps(dict(state='verified',verified_at=now.isoformat(),day=selected.isoformat(),
    calendar_matches=len(calendar['rows']),historical_profiles=original_count,
    participating_profiles=expected['participating_profiles'],filtered_profiles=len(expected['rows']),
    missing_profiles=expected['missing_profiles'],exported_profiles=len(exported),
    exact_ids_and_competitions=True,filter_switch_checked=True,native_ui_ok=True,
    api_requests=0,production_database_writes=0),ensure_ascii=False))
