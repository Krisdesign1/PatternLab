"""Causal, local replay of daily group construction, never a prospective record.

Final calendar identities are reconstructed; outcomes are hidden from planning.
An incremental history index reproduces the fixed Clubs estimator without
loading trained artifacts or repeatedly scanning every historic score.
"""
from collections import Counter, defaultdict
from datetime import date, datetime, time, timezone
from hashlib import sha256
import json
from math import fsum, log
from zoneinfo import ZoneInfo

import api_football as api
import api_match_workspace as workspace
import daily_analysis_engine as engine
from coupon_sizes import top_independent
from league_model import VALUES, score
from match_predictor import MIN_DAYS, MIN_LEAGUE, MIN_TEAM, SHRINK, half_probabilities
from scenario_predictor import MIN_TRAIN


VERSION = 'daily-selection-replay-v1'
ZONE = ZoneInfo('Africa/Kinshasa')
BINS = (0, 50, 65, 80, 100)


def _date(value):
    if type(value) is date:
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ValueError('La période de rejeu doit préciser des dates locales valides.')


def _order(row):
    return row['timestamp'], int(row['match_id'])


def _league():
    return dict(matches=0, days=set(), counts=Counter(), home=Counter(), away=Counter(),
                home_roles={}, away_roles={}, home_goals=[0,0], away_goals=[0,0])


def _role():
    return dict(matches=0, scored=[0,0], conceded=[0,0])


class HistoryIndex:
    """Incrementally include only complete results before a common UTC date."""
    def __init__(self, rows):
        self.rows = sorted(rows, key=_order)
        self.position = 0
        self.before = None
        self.leagues = defaultdict(_league)

    def advance(self, before):
        if self.before is not None and before < self.before:
            raise ValueError('Les coupures du rejeu doivent avancer chronologiquement.')
        while self.position < len(self.rows) and self.rows[self.position]['timestamp'][:10] < before:
            row = self.rows[self.position]
            ht, ft = score(row['ht']), score(row['ft'])
            home = (ht[0], ft[0]-ht[0]);away = (ht[1], ft[1]-ht[1])
            league = self.leagues[row['league']]
            league['matches'] += 1;league['days'].add(row['timestamp'][:10]);league['counts'][row['value']] += 1
            league['home'][row['home']] += 1;league['away'][row['away']] += 1
            for role, team, scored, conceded in [('home_roles',row['home'],home,away),('away_roles',row['away'],away,home)]:
                profile = league[role].setdefault(team,_role())
                profile['matches'] += 1
                for half in range(2):
                    profile['scored'][half] += scored[half];profile['conceded'][half] += conceded[half]
            for half in range(2):
                league['home_goals'][half] += home[half];league['away_goals'][half] += away[half]
            self.position += 1
        self.before = before

    def estimate(self, fixture):
        league = self.leagues[fixture['league']]
        n = league['matches']
        baseline = {value:(league['counts'][value]+1)/(n+3) for value in VALUES}
        home = league['home_roles'].get(fixture['home'],_role())
        away = league['away_roles'].get(fixture['away'],_role())
        eligible = n >= MIN_LEAGUE and len(league['days']) >= MIN_DAYS
        personalized = eligible and min(home['matches'],away['matches']) >= MIN_TEAM
        probabilities = baseline.copy()
        if personalized:
            means=[]
            for half in range(2):
                hm=league['home_goals'][half]/n;am=league['away_goals'][half]/n
                hs=(home['scored'][half]+SHRINK*hm)/(home['matches']+SHRINK)
                ac=(away['conceded'][half]+SHRINK*hm)/(away['matches']+SHRINK)
                ass=(away['scored'][half]+SHRINK*am)/(away['matches']+SHRINK)
                hc=(home['conceded'][half]+SHRINK*am)/(home['matches']+SHRINK)
                means.append((hs+ac+ass+hc)/2)
            team=half_probabilities(*means)
            weight=min(home['matches'],away['matches'])/(min(home['matches'],away['matches'])+SHRINK)
            probabilities={value:weight*team[value]+(1-weight)*baseline[value] for value in VALUES}
        return dict(probabilities=probabilities,baseline=baseline,eligible=eligible,personalized=personalized,
                    training=n,home_profile=home,away_profile=away,before=self.before)


def construct(eligible, supports):
    """Mirror v1 partitions/buckets/coverage exactly; tested against its planner."""
    if engine.VERSION != 'daily-data-support-partitions-v1':
        raise ValueError('La politique quotidienne a changé ; son rejeu doit être revu.')
    groups={}
    def add(members):
        if 2 <= len(members) <= 20:
            ordered=sorted(members,key=_order)
            groups.setdefault(tuple(sorted(row['match_id'] for row in members)),ordered)
    ranked=sorted(eligible,key=lambda row:(-supports[row['match_id']]['data_support_index'],*_order(row)))
    for size in range(2,min(20,len(ranked))+1):
        for index in range(0,len(ranked)-size+1,size):add(ranked[index:index+size])
    buckets=defaultdict(list)
    for row in sorted(eligible,key=_order):
        stamp=datetime.fromisoformat(row['timestamp']).replace(tzinfo=timezone.utc)
        buckets[stamp.astimezone(ZONE).hour//engine.BUCKET_HOURS].append(row)
    for _,members in sorted(buckets.items()):
        for index in range(0,len(members),20):add(members[index:index+20])
    covered={row['match_id'] for group in groups.values() for row in group}
    for row in sorted(eligible,key=_order):
        if row['match_id'] in covered or len(eligible)<2:continue
        stamp=datetime.fromisoformat(row['timestamp'])
        partner=min((other for other in eligible if other['match_id']!=row['match_id']),
                    key=lambda other:(abs((datetime.fromisoformat(other['timestamp'])-stamp).total_seconds()),*_order(other)))
        add([row,partner]);covered.update([row['match_id'],partner['match_id']])
    return list(groups.values())


def _available(group, index):
    counts=Counter(row['league'] for row in group)
    return all(count==1 or index.leagues[league]['matches']//count>=MIN_TRAIN for league,count in counts.items())


def _score(group, estimates, outcomes, name='probabilities'):
    actual=tuple(outcomes[row['match_id']]['value'] for row in group)
    distributions=[estimates[row['match_id']][name] for row in group]
    ranking=[tuple(row['sequence']) for row in top_independent(distributions,3)]
    return dict(top1=int(actual==ranking[0]),top3=int(actual in ranking),
                loss=fsum(-log(p[value]) for p,value in zip(distributions,actual)))


def _aggregate():
    return dict(groups=0,evaluated=0,unavailable=0,missing_outcomes=0,
                clubs_top1=0,clubs_top3=0,clubs_loss_sum=0.,
                baseline_top1=0,baseline_top3=0,baseline_loss_sum=0.,days=set())


def _production(rows):
    output=[]
    for size,row in sorted(rows.items()):
        n=row['evaluated']
        result={key:value for key,value in row.items() if key!='days'}
        result.update(size=size,days=len(row['days']))
        for method in ('clubs','baseline'):
            for rank in ('top1','top3'):
                result[f'{method}_{rank}_rate']=row[f'{method}_{rank}']/n if n else None
            result[f'{method}_loss_per_match']=row[f'{method}_loss_sum']/(n*size) if n else None
        output.append(result)
    return output


def evaluate(raw, *, today, start='2022-01-01', end=None, progress=None):
    """Replay every available past day, using censored calendars and fixed rules."""
    today,start=_date(today),_date(start)
    end=today if end is None else _date(end)
    if start>=end or end>today:raise ValueError('La fin du rejeu est exclusive et ne dépasse pas aujourd’hui.')
    unique={}
    for item in raw:
        try:
            if not isinstance(item,dict) or item.get('provider') not in (None,'api-football','API-Football'):
                raise ValueError('Seules les données API-Football sont acceptées.')
            identity=api._numeric_id(item['fixture']['id'],'du match')
        except (KeyError,TypeError):
            continue
        if identity in unique and unique[identity]!=item:raise ValueError('Identifiant contradictoire dans le rejeu.')
        unique[identity]=item
    history=[];outcomes={};calendars=defaultdict(list);invalid=0;missing=0;years=Counter()
    for identity,item in unique.items():
        try:
            fixture=workspace.fixture_from_raw(item)
            stamp=workspace._stamp(item);day=stamp.astimezone(ZONE).date()
        except (KeyError,TypeError,ValueError,AttributeError):
            invalid+=1;continue
        if day>=today:continue
        if start<=day<end and stamp>datetime.combine(day,time.min,tzinfo=ZONE).astimezone(timezone.utc):
            calendars[day].append(fixture)
        if api._status_short(item) in api.FINISHED_STATUSES:
            try:
                normalized=api.normalize_fixture(item)
                history.append(normalized);outcomes[identity]=normalized
                if start<=day<end:years[day.year]+=1
            except (ValueError,TypeError,KeyError):missing+=1
    days=sorted(calendars);index=HistoryIndex(history)
    production=defaultdict(_aggregate);paired=[];attrition=Counter();evaluated_days=0;eligible_total=0
    bins=[dict(lower=lo,upper=hi,matches=0,missing_outcomes=0,clubs_loss_sum=0.,baseline_loss_sum=0.)
          for lo,hi in zip(BINS,BINS[1:])]
    for number,day in enumerate(days,1):
        slate=sorted(calendars[day],key=_order)
        before=min(row['timestamp'][:10] for row in slate)
        index.advance(before)
        supports={row['match_id']:engine._support(row,index.leagues) for row in slate}
        eligible=[row for row in slate if supports[row['match_id']]['eligible']]
        eligible_total+=len(eligible)
        estimates={row['match_id']:index.estimate(row) for row in eligible}
        if eligible:evaluated_days+=1
        for fixture in eligible:
            mid=fixture['match_id'];support=supports[mid]['data_support_index']
            bucket=next(row for row in bins if row['lower']<=support and (support<row['upper'] or row['upper']==100))
            if mid not in outcomes:
                bucket['missing_outcomes']+=1;continue
            actual=outcomes[mid]['value'];estimate=estimates[mid]
            bucket['matches']+=1
            bucket['clubs_loss_sum']+=-log(estimate['probabilities'][actual])
            bucket['baseline_loss_sum']+=-log(estimate['baseline'][actual])
        for group in construct(eligible,supports):
            row=production[len(group)];row['groups']+=1
            if not _available(group,index):row['unavailable']+=1;continue
            if any(item['match_id'] not in outcomes for item in group):row['missing_outcomes']+=1;continue
            row['evaluated']+=1;row['days'].add(day.isoformat())
            for method,name in [('clubs','probabilities'),('baseline','baseline')]:
                result=_score(group,estimates,outcomes,name)
                for rank in ('top1','top3'):row[f'{method}_{rank}']+=result[rank]
                row[f'{method}_loss_sum']+=result['loss']
        # Trim a common chronological remainder BEFORE either grouping order.
        for size in range(2,min(20,len(eligible))+1):
            pool=eligible[:len(eligible)//size*size]
            if any(item['match_id'] not in outcomes for item in pool):
                attrition['day_sizes_missing_outcomes']+=1;continue
            ordered=sorted(pool,key=lambda row:(-supports[row['match_id']]['data_support_index'],*_order(row)))
            support_groups=[sorted(ordered[pos:pos+size],key=_order) for pos in range(0,len(pool),size)]
            reference_groups=[pool[pos:pos+size] for pos in range(0,len(pool),size)]
            if any(not _available(group,index) for group in support_groups+reference_groups):
                attrition['day_sizes_unavailable_method']+=1;continue
            left=Counter(item['match_id'] for group in support_groups for item in group)
            right=Counter(item['match_id'] for group in reference_groups for item in group)
            if left!=right:raise ValueError('Les deux constructions n’utilisent pas les mêmes rencontres.')
            record=dict(day=day.isoformat(),size=size,groups=len(reference_groups),member_sets_equal=True)
            for policy,groups in [('support',support_groups),('reference',reference_groups)]:
                scores=[_score(group,estimates,outcomes) for group in groups]
                record.update({f'{policy}_top1':sum(item['top1'] for item in scores),
                               f'{policy}_top3':sum(item['top3'] for item in scores),
                               f'{policy}_loss_sum':fsum(item['loss'] for item in scores)})
            paired.append(record)
        if progress is not None and (number==1 or number%25==0 or number==len(days)):
            progress(dict(days_completed=number,days_total=len(days),day=day.isoformat(),
                          evaluated_day_sizes=len(paired),eligible_matches=eligible_total))
    for row in bins:
        row['clubs_loss']=row['clubs_loss_sum']/row['matches'] if row['matches'] else None
        row['baseline_loss']=row['baseline_loss_sum']/row['matches'] if row['matches'] else None
    protocol=dict(version=VERSION,start=start.isoformat(),end_exclusive=end.isoformat(),today=today.isoformat(),
                  selection_policy=engine.VERSION,model='half-goals-v1 independent Clubs',sizes=list(range(2,21)),
                  history_cutoff='strictly_before_first_target_UTC_day',
                  calendar='final_local_identities_reconstructed_as_NS_at_Kinshasa_day_start',
                  controlled_remainder='common_chronological_remainder_removed_before_both_sorts',
                  support_bins=list(BINS),artifact_reads=False,tuning=False)
    inventory=dict(raw_fixtures=len(unique),calendar_days=len(days),days_with_eligible_matches=evaluated_days,
                   calendar_matches=sum(map(len,calendars.values())),eligible_matches=eligible_total,
                   valid_past_results=len(history),invalid_fixtures=invalid,invalid_final_scores=missing,
                   target_results_by_year={str(year):count for year,count in sorted(years.items())})
    return dict(version=VERSION,protocol=protocol,inventory=inventory,counts=inventory,
                production_by_size=_production(production),paired_records=paired,
                controlled_attrition=dict(attrition),support_bins=bins,
                warnings=['Calendriers reconstruits : leur disponibilité, leurs statuts et leurs corrections avant match ne sont pas connus.',
                          'Rejeu exploratoire sur les données actuellement présentes ; aucun nouveau test indépendant n’est créé.',
                          'Clubs est évalué avec ses règles de disponibilité ; aucun artefact CatBoost ou de contexte actuel n’est lu.',
                          'Les taux par taille ne doivent pas être regroupés en une précision globale.',
                          'Les tranches d’appui décrivent des populations différentes ; elles ne prouvent pas un effet causal de l’indice.'])
