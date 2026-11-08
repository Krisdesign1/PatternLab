"""Read-only explanations of saved result states, shared by every history.

Local provider observations explain a blockage; they never settle a forecast,
alter a verdict or feed a success rate. Older immutable records remain readable.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from zoneinfo import ZoneInfo
from halftime_domain import FINISHED_STATUSES, parse_score, outcome


FINISHED = FINISHED_STATUSES
LABELS = {
    'PST': ('postponed', 'Reporté', 'Le fournisseur signale un report.'),
    'CANC': ('cancelled', 'Annulé', 'Le fournisseur signale une annulation.'),
    'ABD': ('abandoned', 'Abandonné', 'Le fournisseur signale un abandon.'),
    'AWD': ('awarded', 'Décision administrative', 'Résultat attribué administrativement ; pas de résultat par mi-temps évaluable.'),
    'WO': ('walkover', 'Forfait', 'Le fournisseur signale un forfait ; pas de résultat par mi-temps évaluable.'),
    'SUSP': ('suspended', 'Suspendu', 'Le fournisseur signale une suspension.'),
    'INT': ('interrupted', 'Interrompu', 'Le fournisseur signale une interruption.'),
}
LIVE = {'1H': 'Première mi-temps', 'HT': 'Mi-temps', '2H': 'Seconde mi-temps',
        'ET': 'Prolongation', 'BT': 'Pause de prolongation', 'P': 'Tirs au but', 'LIVE': 'En cours'}
ZONE = ZoneInfo('Africa/Kinshasa')


def _stamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None


def _score(value):
    try:
        return parse_score(value)
    except ValueError:
        return None


def _valid_scores(ht, ft):
    try:
        outcome(ht, ft)
        return True
    except ValueError:
        return False


def _fixtures(entry):
    values = entry.get('fixtures')
    if not isinstance(values, (list, tuple)):
        values = [entry['fixture']] if isinstance(entry.get('fixture'), dict) else []
    return [row for row in values if isinstance(row, dict)]


def _issue_state(message):
    """Classify old saved explanations without inventing a sporting result."""
    text = message.casefold()
    if any(value in text for value in ('horaire modifié', 'nouvel horaire', 'correction horaire')):
        return 'schedule_changed', 'Horaire modifié', message
    if any(value in text for value in ('identifiants', 'les clubs, la compétition')):
        return 'identity_review', 'Affiche modifiée', message
    if 'contradictoir' in text or 'conflit' in text:
        return 'conflicting_observations', 'Relevés contradictoires', message
    if 'absent du relevé' in text:
        return 'missing_observation', 'Match absent du relevé', message
    return 'unresolved', 'Résultat non évaluable', message


def _issues(entry, mid):
    result = []
    for issue in entry.get('issues') or []:
        if not isinstance(issue, str):
            continue
        identifiers = re.findall(r'\bMatch\s+(\d+)\b', issue, flags=re.I)
        if not identifiers or mid in identifiers:
            result.append(issue)
    return result


def _saved_final_blocker(entry, fixture, raw, issues):
    """Name an explicit saved rejection; complete scores do not clear it.

    This only explains the last observation. In particular, a different kickoff
    can remain inadmissible under the saved protocol even when the capture was
    before both kickoffs. No settlement rule is inferred or changed here.
    """
    for issue in issues:
        state, label, reason = _issue_state(issue)
        text = issue.casefold()
        if state == 'schedule_changed':
            captured = _stamp(entry.get('created_at') or entry.get('captured_at'))
            original = _stamp(fixture.get('timestamp'))
            effective = _stamp(raw.get('fixture', {}).get('date'))
            if captured is not None and original is not None and effective is not None:
                if captured >= min(original, effective):
                    return ('noncausal_forecast', 'Prévision hors période avant match',
                            reason + ' L’enregistrement de la prévision ne précède pas les deux horaires '
                            'de coup d’envoi ; elle reste exclue de l’évaluation avant match.')
            if 'résultat final confirmé nécessaire' in text:
                # A complete final supplies precisely the missing evidence for
                # the normal schedule resolver; this is not a saved rejection
                # of that final under a stricter protocol or causality rule.
                continue
            return ('schedule_changed', 'Horaire à examiner',
                    reason + ' Des scores complets ne lèvent pas le blocage horaire enregistré ; '
                    'le résultat reste à examiner selon les règles de cette prévision.')
        if state in ('identity_review', 'conflicting_observations'):
            return state, label, reason
        if 'identité' in text or 'identite' in text:
            return 'identity_review', 'Identité à vérifier', reason
        if 'protocole' in text and any(word in text for word in ('figé', 'incompatible', 'modifié')):
            return 'protocol_review', 'Protocole à vérifier', reason
    return None


def _saved_observation(entry, fixture, current):
    mid = str(fixture.get('match_id'))
    def valid(row):
        if not _valid_scores(row.get('ht'), row.get('ft')):
            return False
        actual = outcome(row['ht'], row['ft'])
        for key in ('value', 'actual'):
            if row.get(key) is not None and row[key] != actual:
                return False
        checked = _stamp(entry.get('checked_at'))
        correction = row.get('schedule_resolution') or {}
        kickoff = _stamp(correction.get('effective_timestamp')) if isinstance(correction, dict) else None
        kickoff = kickoff or _stamp(fixture.get('timestamp'))
        if entry.get('checked_at') is not None and (checked is None or checked > current or
                                                   kickoff is not None and checked < kickoff):
            return False
        return True
    observed = entry.get('observed') or []
    found = [row for row in observed if isinstance(row, dict) and str(row.get('match_id')) == mid]
    if len(found) == 1 and valid(found[0]):
        return found[0]
    if 'fixture' in entry and str(entry['fixture'].get('match_id')) == mid:
        if entry.get('status') == 'settled' and valid(entry):
            return entry
    return None


def _match(entry, fixture, raw, current):
    mid = str(fixture.get('match_id', ''))
    result = dict(match_id=mid, home=fixture.get('home', '—'), away=fixture.get('away', '—'),
                  raw_status=None, state='unresolved', label='À vérifier', reason='',
                  checked_at=entry.get('checked_at'))

    def finish(state, label, reason):
        return dict(result, state=state, label=label, reason=reason)

    if raw is not None:
        from fixture_import_policy import quality_error
        conflict = quality_error(raw)
        if conflict:
            return finish('import_quarantine', 'Correction en quarantaine',
                          conflict + ' Les prévisions et verdicts déjà enregistrés ne sont pas réécrits par cette consultation.')
    # Keep the saved observation as the authority for evaluated results.
    if _saved_observation(entry, fixture, current) is not None:
        return finish('evaluated', 'Évalué', 'Scores exploitables conservés dans le journal.')
    issues = _issues(entry, mid)
    message = ' '.join(issues)
    if raw is not None:
        try:
            if (raw.get('provider') not in (None, 'api-football', 'API-Football') or
                    str(raw['fixture']['id']) != mid or
                    any(key in fixture and str(fixture[key]) != str(value) for key, value in (
                        ('league', raw['league']['id']), ('home_id', raw['teams']['home']['id']),
                        ('away_id', raw['teams']['away']['id'])))):
                return finish('identity_mismatch', 'Identité à vérifier',
                              'Les identifiants du relevé local ne correspondent pas à la prévision figée.')
            short = raw['fixture']['status']['short']
            result['raw_status'] = short
            if short in LABELS:
                return finish(*LABELS[short])
            if short in FINISHED:
                kickoff = _stamp(raw['fixture'].get('date'))
                if kickoff is not None and kickoff > current:
                    return finish('invalid_observation', 'Résultat à vérifier', 'Le relevé annonce un résultat final avant son coup d’envoi ; aucun verdict n’est déduit.')
                scores = raw.get('score') or {}
                ht, ft = scores.get('halftime'), scores.get('fulltime')
                # Regulation time is required even for AET/PEN.
                if _score(ht) is None and _score(ft) is None:
                    return finish('missing_scores', 'Scores manquants', 'Match déclaré terminé, mais scores à la pause et à 90 minutes absents ou invalides.')
                if _score(ht) is None:
                    return finish('missing_halftime', 'Score à la pause manquant', 'Match déclaré terminé ; le score à la pause manque. Impossible de déduire 1st / Égal / 2nd du seul score final.')
                if _score(ft) is None:
                    return finish('missing_fulltime', 'Score à 90 minutes manquant', 'Match déclaré terminé ; le score à 90 minutes manque. Les prolongations et tirs au but ne le remplacent pas.')
                if not _valid_scores(ht, ft):
                    return finish('invalid_scores', 'Scores incohérents', 'Le score à la pause est incompatible avec celui à 90 minutes.')
                blocker = _saved_final_blocker(entry, fixture, raw, issues)
                if blocker is not None:
                    return finish(*blocker)
                return finish('awaiting_reconciliation', 'Résultat à rapprocher',
                              'Scores disponibles dans le dernier relevé local ; le résultat enregistré reste à actualiser ou à vérifier.' + (' ' + message if message else ''))
            if short == 'TBD':
                return finish('unconfirmed_schedule', 'Horaire non confirmé', 'Le fournisseur ne confirme pas encore la date ou l’heure.')
            kickoff = _stamp(raw['fixture'].get('date')) or _stamp(fixture.get('timestamp'))
            original = _stamp(fixture.get('timestamp'))
            if kickoff is not None and original is not None and kickoff != original:
                before = original.astimezone(ZONE).strftime('%d/%m/%Y à %H:%M')
                after = kickoff.astimezone(ZONE).strftime('%d/%m/%Y à %H:%M')
                label = ('Reprogrammé au ' + kickoff.astimezone(ZONE).strftime('%d/%m à %H:%M')
                         if short == 'NS' and kickoff > current else 'Horaire modifié')
                return finish('rescheduled', label,
                              f'Horaire annoncé modifié : {before} → {after} (Kinshasa). '
                              f'Dernier statut local : {short or "inconnu"}. Le résultat reste non évalué ; '
                              'la prévision et son horaire d’origine sont conservés.')
            if kickoff is not None and current > kickoff + timedelta(hours=6):
                return finish('stale_result', 'Résultat final non reçu',
                              f'Dernier statut local : {short or "inconnu"}. Plus de six heures après le coup d’envoi annoncé, aucun résultat final exploitable n’est reçu ; cela ne prouve ni un report ni un échec.')
            if short in LIVE:
                return finish('in_progress', LIVE[short], 'Statut du dernier relevé local ; aucun résultat final évaluable enregistré.')
            if message:
                return finish(*_issue_state(message))
            if kickoff is not None and kickoff > current:
                return finish('upcoming', 'À venir', 'Le coup d’envoi annoncé n’a pas encore eu lieu.')
            return finish('awaiting_result', 'Résultat attendu', 'Résultat final absent du dernier relevé local.')
        except (KeyError, TypeError, AttributeError):
            return finish('invalid_observation', 'Relevé incomplet', 'Le relevé local est incomplet ; le résultat enregistré ne peut pas être évalué à partir de ce relevé.')
    # Old journal events have only text issues. Do not invent a precise subtype.
    if 'reporté ou interrompu' in message:
        return finish('postponed_or_interrupted', 'Reporté ou interrompu', message)
    if 'annulé, abandonné ou attribué' in message:
        return finish('void', 'Non évaluable', message)
    if 'Score HT/FT absent ou invalide' in message:
        return finish('missing_scores', 'Scores incomplets', message)
    if message:
        return finish(*_issue_state(message))
    kickoff = _stamp(fixture.get('timestamp'))
    if entry.get('status') == 'void':
        return finish('void', 'Non évaluable', 'Le groupe est exclu de l’évaluation ; le motif précis de ce match n’est pas conservé.')
    if kickoff is not None and kickoff > current:
        return finish('upcoming', 'À venir', 'Le coup d’envoi enregistré n’a pas encore eu lieu.')
    return finish('awaiting_result', 'Résultat non reçu', 'Aucun résultat exploitable conservé pour cette rencontre ; son ancienne date ne suffit pas à déterminer son issue.')


def describe_entry(entry, *, raw_by_id=None, now=None):
    """Explain without changing lifecycle, saved scores, forecasts or verdicts."""
    current = datetime.now(timezone.utc) if now is None else now
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise ValueError('Une heure de consultation avec fuseau est nécessaire.')
    raw_by_id = raw_by_id or {}
    matches = [_match(entry, fixture, raw_by_id.get(str(fixture.get('match_id'))), current)
               for fixture in _fixtures(entry)]
    blocking = [row for row in matches if row['state'] != 'evaluated']
    counts = Counter(row['label'] for row in blocking)
    label = next(iter(counts)) if len(counts) == 1 else 'Plusieurs motifs' if counts else 'Évalué'
    summary = ' · '.join(f"{row['home']} — {row['away']} : {row['label']}" for row in blocking[:2])
    if len(blocking) > 2:
        summary += f' · +{len(blocking)-2} autre(s) rencontre(s)'
    if not summary:
        summary = 'Tous les résultats exploitables sont conservés.' if matches else 'Détails des rencontres indisponibles.'
    if not matches:
        label = 'Détails indisponibles'
    return dict(label=label, summary=summary, matches=matches, blocking=blocking, projection_only=True)


def annotate_entries(entries, *, root=None, now=None):
    """One bounded local query for requested entries; no API or storage write.

    Respect STORE_PATH so isolated tests and committed cloud snapshots do not
    accidentally consult the production directory. Readers remain independent
    of the reconciliation/settlement operation.
    """
    entries = list(entries)
    ids = {str(fixture.get('match_id')) for entry in entries
           for fixture in _fixtures(entry)}
    raw_by_id, unavailable = {}, False
    if ids:
        import api_football as api
        path = Path(root) / 'fixtures.json' if root is not None else api.STORE_PATH
        try:
            for raw in api.load_fixtures(path, ids=sorted(ids)):
                mid = str(raw.get('fixture', {}).get('id'))
                if mid in ids:
                    if mid in raw_by_id and raw_by_id[mid] != raw:
                        raw_by_id[mid] = {}  # conflicting observations fail closed
                    else:
                        raw_by_id[mid] = raw
        except (api.ApiFootballError, OSError, ValueError, TypeError):
            unavailable = True
    output = []
    for entry in entries:
        explanation = describe_entry(entry, raw_by_id=raw_by_id, now=now)
        if unavailable:
            explanation['local_unavailable'] = True
        output.append(dict(entry, result_explanation=explanation))
    return output
