"""Independent local statistics collector. No serving-model or Cloud imports."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))


def watch_delay(action,result,active_interval,idle_interval,*,now=None):
    """Continue useful work promptly; never spin on idle or provider pauses."""
    current=time.time() if now is None else now
    queue=result.get('queue',{})
    if queue.get('provider_pause_until',0)>current:
        return idle_interval
    if (result.get('status')=='completed' and result.get('requests',0)>0
        and action=='run'):
        return active_interval
    return idle_interval


def main(argv=None):
    parser=argparse.ArgumentParser(description='Collecte locale reprenable des statistiques par mi-temps.')
    parser.add_argument('action',choices=('status','plan','import-audit','discover','run'))
    parser.add_argument('--database',type=Path,default=ROOT/'data/api_football/halftime_statistics.sqlite3')
    parser.add_argument('--source',type=Path,default=ROOT/'data/api_football/fixtures.sqlite3')
    parser.add_argument('--audit-directory',type=Path,default=ROOT/'data/research/halftime_coverage/2026-10-26-v1')
    parser.add_argument('--leagues',default='39,40,61,78,135,140,71',help='IDs séparés par virgules, ou all pour toutes les compétitions couvertes.')
    parser.add_argument('--seasons',default='2024,2025,2026')
    parser.add_argument('--batch-size',type=int,default=25,help='Taille du passage, pas une limite de collecte totale.')
    parser.add_argument('--watch',action='store_true',help='Reprendre les lots automatiquement, y compris après une pause fournisseur.')
    parser.add_argument('--interval-seconds',type=int,default=0,help='Pause entre les lots actifs ; zéro continue sans attente fixe, dans les quotas API.')
    parser.add_argument('--idle-interval-seconds',type=int,default=60,help='Attente lorsque la file est vide ou les appels API sont en pause.')
    args=parser.parse_args(argv)
    if args.batch_size<1 or args.interval_seconds<0 or args.idle_interval_seconds<1:
        parser.error('Taille de lot et attente inactive positives ; pause active positive ou nulle.')
    try:
        leagues=None if args.leagues=='all' else tuple(str(int(v)) for v in args.leagues.split(','))
        seasons=tuple(int(v) for v in args.seasons.split(','))
        if (leagues is not None and any(int(v)<1 for v in leagues)) or any(v<2024 for v in seasons):
            parser.error('Identifiants positifs et saisons à partir de 2024 requis.')
    except ValueError:
        parser.error('Identifiants ou saisons invalides.')
    from halftime_statistics_store import Store
    from halftime_statistics_collector import plan_local, seed_audit, discover, collect_once
    from halftime_statistics_client import ApiClient
    try:
        with Store(args.database,readonly=args.action=='status') as store:
            client=None
            while True:
                if args.action=='status':
                    result=store.status()
                elif args.action=='plan':
                    result=plan_local(store,args.source,leagues,seasons)
                elif args.action=='import-audit':
                    result=seed_audit(store,args.audit_directory)
                else:
                    client=client or ApiClient(root=ROOT)
                    if (args.action=='run' and args.watch
                        and time.time()-store.get_meta('last_discovery_cycle',0)>=86400):
                        discovery=discover(store,client,leagues,seasons)
                        print(json.dumps(dict(action='daily_discovery',result=discovery),ensure_ascii=False),flush=True)
                    result=(discover(store,client,leagues,seasons) if args.action=='discover'
                            else collect_once(store,client,args.batch_size))
                print(json.dumps(dict(at_utc=datetime.now(timezone.utc).isoformat(),action=args.action,result=result),
                                 ensure_ascii=False,allow_nan=False),flush=True)
                if not args.watch or args.action not in ('run','discover'):
                    break
                time.sleep(watch_delay(args.action,result,args.interval_seconds,args.idle_interval_seconds))
        return 0
    except KeyboardInterrupt:
        print(json.dumps(dict(status='stopped',message='Arrêt demandé ; la file reprendra au prochain lancement.')),flush=True)
        return 0
    except Exception:
        # Client/store errors never expose credentials or provider echo messages.
        print(json.dumps(dict(status='error',message='Collecte interrompue ; les réponses validées restent conservées. Vérifier réseau, configuration et stockage local.')),file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
