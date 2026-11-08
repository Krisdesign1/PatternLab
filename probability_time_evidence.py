"""Qualified timing evidence; a local read is not historical publication proof."""
from copy import deepcopy
from datetime import date
from fixture_temporal import utc_instant


VERSION = 'probability-time-evidence-v1'


def _revisions(receipt):
    values=[receipt.get(key) for key in ('store_revision_start','store_revision_end')]
    if (any(not isinstance(value,list) or len(value)!=2 for value in values)
            or receipt.get('consistent_store_revision')!=(values[0]==values[1])):
        raise ValueError('Les révisions conservées ne correspondent pas à la qualification de lecture.')


def read_receipt(started, finished, signature_start, signature_end, before, rows):
    started,finished=utc_instant(started),utc_instant(finished)
    if finished<started or date.fromisoformat(before).isoformat()!=before:
        raise ValueError('Chronologie de lecture des sources invalide.')
    # Signatures omit the local filesystem path, which is not useful in exports.
    return dict(version=VERSION,read_started_at=started.isoformat(),read_finished_at=finished.isoformat(),
                store_revision_start=list(signature_start[1:]),store_revision_end=list(signature_end[1:]),
                consistent_store_revision=tuple(signature_start)==tuple(signature_end),
                history_cutoff_exclusive=before,
                history_fingerprints={row['fixture']['league']:row['history_fingerprint'] for row in rows})


def qualify(report, row, current, *, isolated_clock=False):
    """Bind real read times to a new capture, without upgrading old evidence."""
    receipt=report.get('source_read_receipt')
    result=dict(version=VERSION,quality='source_times_unattested',strict_as_of_used=False,
                historical_availability_proven=False,provider_publication_time_proven=False)
    if receipt is None:
        return result
    if (not isinstance(receipt,dict) or receipt.get('version')!=VERSION
            or type(receipt.get('consistent_store_revision')) is not bool
            or receipt.get('history_cutoff_exclusive')!=row.get('before',report.get('before'))):
        raise ValueError('Preuve de lecture des sources invalide.')
    _revisions(receipt)
    started,finished=utc_instant(receipt.get('read_started_at')),utc_instant(receipt.get('read_finished_at'))
    if finished<started:
        raise ValueError('Chronologie de lecture des sources invalide.')
    league=str(row['fixture']['league'])
    fingerprint=row.get('history_fingerprint') or report.get('history_fingerprint')
    fingerprints=receipt.get('history_fingerprints')
    if not isinstance(fingerprints,dict) or fingerprints.get(league)!=fingerprint:
        raise ValueError('L’historique ne correspond pas à la preuve de lecture.')
    if isolated_clock:
        # The archive permits injected clocks outside the production directory.
        # Neither past nor future synthetic clocks can attest a real read time.
        return dict(result,quality='isolated_clock_unattested')
    if finished>utc_instant(current):
        raise ValueError('Les sources ont été lues après l’heure de capture.')
    evidence={key:deepcopy(value) for key,value in receipt.items() if key!='history_fingerprints'}
    evidence.update(league=league,history_fingerprint=fingerprint)
    result.update(quality='local_read_window' if receipt['consistent_store_revision'] else 'concurrent_store_read',
                  source_read=evidence)
    return result


def validate_saved(value, *, created_at, before, league, history_fingerprint):
    """Check only the saved evidence; never consult current sources or clocks."""
    if (not isinstance(value,dict) or value.get('version')!=VERSION
            or value.get('quality') not in ('source_times_unattested','isolated_clock_unattested',
                                          'local_read_window','concurrent_store_read')
            or any(value.get(key) is not False for key in ('strict_as_of_used',
                'historical_availability_proven','provider_publication_time_proven'))):
        raise ValueError('Qualification temporelle invalide.')
    receipt=value.get('source_read')
    if value['quality'] in ('source_times_unattested','isolated_clock_unattested'):
        if receipt is not None:
            raise ValueError('Une source non attestée ne peut pas fournir de preuve de lecture.')
        return
    if (not isinstance(receipt,dict) or receipt.get('version')!=VERSION
            or receipt.get('league')!=str(league) or receipt.get('history_fingerprint')!=history_fingerprint
            or receipt.get('history_cutoff_exclusive')!=before
            or type(receipt.get('consistent_store_revision')) is not bool
            or (value['quality']=='local_read_window')!=receipt['consistent_store_revision']):
        raise ValueError('La lecture ne correspond pas à la prévision conservée.')
    _revisions(receipt)
    start,end=utc_instant(receipt.get('read_started_at')),utc_instant(receipt.get('read_finished_at'))
    if not start<=end<=utc_instant(created_at):
        raise ValueError('Chronologie de lecture conservée invalide.')


def content_key(value):
    """Read timestamps alone never multiply unchanged probability snapshots."""
    if value is None:
        return None
    return {key:deepcopy(item) for key,item in value.items() if key!='source_read'}
