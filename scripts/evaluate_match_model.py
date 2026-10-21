"""Evaluate the chronological club baseline using only local API-Football results."""
import hashlib
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from api_football import load_matches, DATA_DIR
from league_patterns import repeated_scenarios
from match_predictor import backtest, summarize, VERSION


def main():
    rows,audit=load_matches()
    if audit['issues']:
        raise ValueError('Résoudre les incohérences des données avant cette évaluation')
    groups=repeated_scenarios(rows)
    leagues={}
    all_records=[]
    for key,result in groups.items():
        records=backtest(result['matches'],key)
        if not records:
            continue
        first=result['matches'][0]
        leagues[key]={'name':f"{first['country']} — {first['competition']}",
                      'summary':summarize(records),
                      'personalized_summary':summarize([r for r in records if r['personalized']]),
                      'records':records}
        all_records.extend(records)
    report={'source':'API-Football','version':VERSION,'matches':len(rows),
            'data_sha256':hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest(),
            'summary':summarize(all_records),
            'personalized_summary':summarize([r for r in all_records if r['personalized']]),
            'tested_leagues':len(leagues), 'leagues':leagues}
    (DATA_DIR/'match_prediction_audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k!='leagues'},ensure_ascii=False))


if __name__=='__main__':main()
