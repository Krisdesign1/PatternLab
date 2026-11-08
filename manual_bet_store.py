"""Explicit local paper-bet selections, separate from prediction histories.

The proposal snapshot and odds are frozen at addition. Only the user's own
stake and removal are mutable. Reading creates nothing; results are projected
from the current verified scenario catalog, never from a cached old win.
"""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import sqlite3

import bet_simulation as simulation
from scenario_odds import DEFAULT_ODDS


DEFAULT_PATH = Path(__file__).resolve().parent/'data/api_football/manual_bets.sqlite3'
VERSION = 'manual-paper-bet-v1'
TIMINGS = ('before_kickoff', 'retrospective')
SNAPSHOT_FIELDS = ('id','day','entry_id','entry_ids','size','matches','match_ids','sequence','methods',
                   'probability_by_method','captured_at','kickoff_at')


class ManualBetError(ValueError):
    pass


def database_path(path=None):
    return Path(path) if path is not None else DEFAULT_PATH


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def _digest(value):
    return sha256(_json(value).encode()).hexdigest()


def _clock(now):
    value = datetime.now(timezone.utc) if now is None else now
    if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ManualBetError('Une date avec fuseau horaire est nécessaire.')
    return value.astimezone(timezone.utc)


def _amount(value):
    try:
        return simulation._amount(value)
    except ValueError as error:
        raise ManualBetError(str(error)) from None


def _timing(ticket, current):
    try:
        kickoff = simulation._stamp(ticket['kickoff_at'])
    except (KeyError,TypeError,ValueError):
        return 'retrospective'
    return 'before_kickoff' if current < kickoff else 'retrospective'


def _snapshot(ticket,current):
    if not isinstance(ticket,dict) or not isinstance(ticket.get('id'),str) or not ticket['id']:
        raise ManualBetError('Le scénario choisi est absent ou invalide.')
    try:
        size = ticket['size']
        if type(size) is not int or not 2 <= size <= 20:
            raise ValueError()
        sequence = list(simulation._sequence(ticket['sequence'],size))
        if date.fromisoformat(ticket['day']).isoformat() != ticket['day']:
            raise ValueError()
        captured = simulation._stamp(ticket['captured_at'])
        if captured > current:
            raise ValueError()
        if not isinstance(ticket.get('entry_id'),str) or not ticket['entry_id']:
            raise ValueError()
        if not isinstance(ticket.get('matches'),str) or not ticket['matches']:
            raise ValueError()
        value = {key:deepcopy(ticket[key]) for key in SNAPSHOT_FIELDS if key in ticket}
        value['sequence']=sequence
        reference={label:str(DEFAULT_ODDS[label]) for label in simulation.VALUES}
        odds=Decimal(1)
        with localcontext() as context:
            context.prec=512
            for label in sequence:
                price=Decimal(reference[label])
                if not price.is_finite() or price<=1:
                    raise ValueError()
                odds*=price
        if not isfinite(float(odds)):
            raise ValueError()
        value['odds_reference']=reference
        value['odds_decimal']=str(odds)
        value['odds']=float(odds)
        return value
    except (KeyError,TypeError,ValueError,InvalidOperation,OverflowError):
        raise ManualBetError('Le scénario choisi ne peut pas être enregistré.') from None


def _connect(path,*,create=False):
    target=database_path(path)
    if not create:
        if not target.exists():
            return None
        con=sqlite3.connect(target.resolve().as_uri()+'?mode=ro',uri=True,timeout=15)
    else:
        target.parent.mkdir(parents=True,exist_ok=True)
        con=sqlite3.connect(target,timeout=15)
        con.executescript('''
            CREATE TABLE IF NOT EXISTS manual_bets (
                id TEXT PRIMARY KEY, selected_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                timing TEXT NOT NULL, stake TEXT NOT NULL,
                snapshot TEXT NOT NULL, snapshot_digest TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS manual_bets_snapshot_immutable
            BEFORE UPDATE OF id,selected_at,snapshot,snapshot_digest ON manual_bets
            BEGIN SELECT RAISE(ABORT,'The selected proposal is immutable'); END;
        ''')
    con.row_factory=sqlite3.Row
    return con


def _read(row):
    try:
        snapshot=json.loads(row['snapshot'])
        if (not isinstance(snapshot,dict) or snapshot.get('id')!=row['id']
                or _digest(snapshot)!=row['snapshot_digest'] or row['timing'] not in TIMINGS):
            raise ValueError()
        stake=_amount(row['stake'])
        selected=simulation._stamp(row['selected_at']);updated=simulation._stamp(row['updated_at'])
        if updated < selected:
            raise ValueError()
        sequence=simulation._sequence(snapshot['sequence'],snapshot['size'])
        if date.fromisoformat(snapshot['day']).isoformat()!=snapshot['day']:
            raise ValueError()
        reference=snapshot['odds_reference'];odds=Decimal(1)
        with localcontext() as context:
            context.prec=512
            for label in sequence:
                value=Decimal(reference[label])
                if not value.is_finite() or value<=1:
                    raise ValueError()
                odds*=value
        if Decimal(snapshot['odds_decimal'])!=odds or not isfinite(float(odds)):
            raise ValueError()
        if row['timing']=='before_kickoff' and _timing(snapshot,updated)!='before_kickoff':
            raise ValueError()
        return dict(version=VERSION,id=row['id'],selected_at=row['selected_at'],updated_at=row['updated_at'],
            timing=row['timing'],stake=float(stake),stake_decimal=str(stake),
            odds_decimal=snapshot['odds_decimal'],odds_reference=deepcopy(reference),ticket=snapshot)
    except (ValueError,TypeError,KeyError,InvalidOperation,OverflowError):
        raise ManualBetError('Un pari simulé enregistré est incohérent.') from None


def list_bets(path=None):
    """Return only explicit saved selections; an absent database stays absent."""
    con=None
    try:
        con=_connect(path)
        if con is None:return []
        tables={row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables:return []
        if 'manual_bets' not in tables:
            raise ManualBetError('Le portefeuille simulé est invalide.')
        return [_read(row) for row in con.execute('SELECT * FROM manual_bets ORDER BY selected_at,id')]
    except (sqlite3.Error,OSError) as error:
        raise ManualBetError('Impossible de lire le portefeuille simulé.') from error
    finally:
        if con is not None:con.close()


def add_many(tickets,stake,*,path=None,now=None):
    """Save one explicit batch atomically, preserving already chosen stakes."""
    current=_clock(now);money=_amount(stake)
    snapshots={}
    # Validate the whole selection before even creating a storage file. A bad
    # proposal cannot leave a partially added day in the user's portfolio.
    for ticket in tickets:
        snapshot=_snapshot(ticket,current)
        snapshots.setdefault(snapshot['id'],snapshot)
    if not snapshots:
        return []
    con=None
    try:
        con=_connect(path,create=True)
        with con:
            result=[]
            for snapshot in snapshots.values():
                con.execute('INSERT OR IGNORE INTO manual_bets VALUES (?,?,?,?,?,?,?)',
                    (snapshot['id'],current.isoformat(),current.isoformat(),_timing(snapshot,current),
                     str(money),_json(snapshot),_digest(snapshot)))
                row=con.execute('SELECT * FROM manual_bets WHERE id=?',(snapshot['id'],)).fetchone()
                result.append(_read(row))
        return result
    except (sqlite3.Error,OSError) as error:
        raise ManualBetError('Impossible d’ajouter la sélection de paris simulés.') from error
    finally:
        if con is not None:con.close()


def add(ticket,stake,*,path=None,now=None):
    """Add one explicit choice; a repeated click never replaces its stake."""
    return add_many([ticket],stake,path=path,now=now)[0]


def update_stake(ticket_id,stake,*,path=None,now=None):
    """Edit this choice alone; a post-kickoff edit becomes retrospective."""
    current=_clock(now);money=_amount(stake)
    target=database_path(path)
    if not target.exists():raise ManualBetError('Ce pari simulé n’est pas enregistré.')
    con=None
    try:
        # Open an existing file only: editing cannot create an empty ledger.
        con=sqlite3.connect(target.resolve().as_uri()+'?mode=rw',uri=True,timeout=15)
        con.row_factory=sqlite3.Row
        with con:
            row=con.execute('SELECT * FROM manual_bets WHERE id=?',(ticket_id,)).fetchone()
            if row is None:raise ManualBetError('Ce pari simulé n’est pas enregistré.')
            saved=_read(row)
            if current < simulation._stamp(saved['updated_at']):
                raise ManualBetError('La modification précède l’enregistrement du pari.')
            timing=('retrospective' if saved['timing']=='retrospective'
                    or _timing(saved['ticket'],current)=='retrospective' else 'before_kickoff')
            con.execute('UPDATE manual_bets SET stake=?,updated_at=?,timing=? WHERE id=?',
                        (str(money),current.isoformat(),timing,ticket_id))
            result=con.execute('SELECT * FROM manual_bets WHERE id=?',(ticket_id,)).fetchone()
        return _read(result)
    except (sqlite3.Error,OSError) as error:
        raise ManualBetError('Impossible de modifier cette mise.') from error
    finally:
        if con is not None:con.close()


def remove(ticket_id,*,path=None):
    """Remove this manually chosen simulation without touching the journal."""
    target=database_path(path)
    if not target.exists():return False
    con=None
    try:
        con=sqlite3.connect(target.resolve().as_uri()+'?mode=rw',uri=True,timeout=15)
        with con:
            cursor=con.execute('DELETE FROM manual_bets WHERE id=?',(ticket_id,))
        return bool(cursor.rowcount)
    except (sqlite3.Error,OSError) as error:
        raise ManualBetError('Impossible de retirer ce pari simulé.') from error
    finally:
        if con is not None:con.close()


def build(catalog,saved):
    """Value only explicitly saved bets using their own stakes and fixed odds."""
    current={row['id']:row for row in catalog}
    tickets=[];seen=set();duplicates=0
    with localcontext() as context:
        context.prec=512
        for record in saved:
            if record['id'] in seen:
                duplicates+=1;continue
            seen.add(record['id'])
            snapshot=deepcopy(record['ticket']);row=deepcopy(current.get(record['id']) or snapshot)
            # A catalog can represent the same economic choice in another
            # fixture order. Reindex the observed result before restoring the
            # user's frozen presentation order.
            actual=row.get('actual')
            if row.get('status') in ('won','lost') and actual is not None:
                source_ids=row.get('match_ids');selected_ids=snapshot.get('match_ids')
                if source_ids is not None and selected_ids is not None:
                    if (isinstance(source_ids,list) and isinstance(selected_ids,list)
                            and len(source_ids)==len(selected_ids)==len(actual)
                            and len(set(source_ids))==len(source_ids) and set(source_ids)==set(selected_ids)):
                        by_id=dict(zip(source_ids,actual))
                        row['actual']=[by_id[mid] for mid in selected_ids]
                    else:
                        row.update(status='unresolved',actual=None,
                            reason='Les identifiants des rencontres ne correspondent plus au pari choisi.')
                elif (row.get('entry_id')!=snapshot.get('entry_id')
                      or row.get('sequence')!=snapshot.get('sequence')
                      or row.get('matches')!=snapshot.get('matches')):
                    row.update(status='unresolved',actual=None,
                        reason='L’ordre des rencontres ne peut plus être vérifié pour ce pari.')
            # The user's chosen order and original presentation remain fixed.
            for field in SNAPSHOT_FIELDS:
                if field in snapshot:row[field]=deepcopy(snapshot[field])
            if record['id'] not in current:
                row.update(status='unresolved',actual=None,
                    reason='La prévision correspondante n’est plus disponible pour vérifier le résultat.')
            if row.get('status') not in simulation.STATUSES:
                row.update(status='unresolved',actual=None,reason='Le résultat de ce pari n’est pas vérifiable.')
            timing=record['timing']
            current_proposal=current.get(record['id'])
            # A later accepted schedule correction can move kickoff earlier.
            # It must not leave an after-kickoff choice labelled prospective.
            if timing=='before_kickoff' and current_proposal is not None:
                try:
                    selected=simulation._stamp(record['selected_at'])
                    updated=simulation._stamp(record['updated_at'])
                    if _timing(current_proposal,max(selected,updated))!='before_kickoff':
                        timing='retrospective'
                except (ValueError,TypeError,KeyError):
                    timing='retrospective'
            money=_amount(record['stake_decimal']);odds=Decimal(record['odds_decimal'])
            state=row['status']
            returned=((money*odds).quantize(simulation.CENT,rounding=ROUND_HALF_UP) if state=='won'
                      else money if state=='refunded' else Decimal(0) if state=='lost' else None)
            pnl=returned-money if returned is not None else None
            row.update(stake=float(money),odds=float(odds),return_amount=float(returned) if returned is not None else None,
                pnl=float(pnl) if pnl is not None else None,selected_at=record['selected_at'],
                updated_at=record['updated_at'],timing=timing,
                odds_reference=deepcopy(record['odds_reference']),_stake=money,_returned=returned,_pnl=pnl)
            tickets.append(row)
        days=[dict(day=day,**simulation._summary([row for row in tickets if row['day']==day]))
              for day in sorted({row['day'] for row in tickets},reverse=True)]
        summary=simulation._summary(tickets)
        for row in tickets:
            for field in ('_stake','_returned','_pnl'):row.pop(field)
    return dict(tickets=sorted(tickets,key=lambda row:(row['day'],row['selected_at'],row['id']),reverse=True),
        days=days,summary=summary,exclusions={},duplicates_removed=duplicates)
