"""Retire exactly four accepted, superseded deployment/test copies.

Audit is read-only. --apply requires the exact audited plan fingerprint and
repeats all checks before retaining receipts and removing the four copies.
Application data, backups and cutover directories are outside deletion scope.
"""
from datetime import datetime, timezone
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
from uuid import uuid4


LIVE = Path('/opt/patternlab')
MAINTENANCE = Path('/var/lib/patternlab-maintenance')
TARGETS = (
    Path('/var/lib/patternlab-incoming/stage-20261103'),
    Path('/var/lib/patternlab-incoming/stage-20261104-final'),
    Path('/var/lib/patternlab-qualification/replay-20261104'),
    Path('/var/lib/patternlab-qualification/replay-final-20261104'),
)
STAGE_ANCHORS = dict(zip(TARGETS[:2], (
    '2a2a8e2ade62571e01f5a9fab44e2276bd48477e1394ff8fa571b9ff373d15a8',
    '9878ce621d96fcfdbd51067e9a7220e1dda5018c4dda76c3fa8b257d1273f514')))
FIRST_MANIFEST = Path('/var/lib/patternlab-backups/first-aws-20261105/manifest.json')
FIRST_SHA = '1a08f522dbd12d2f7007de2974cc8c949786bd211220f9b594c1543c450f3c4b'
DAILY_ARCHIVE = Path('/var/lib/patternlab-backups/daily-20261106T091115Z-36ab4dee.tar.gz')
DAILY_SHA = 'c16076b3f66fdd567ddd59268ebfa0e0adc8a8da35d5c67a2a757d4e265869e2'
CONFIG_ROOTS = tuple(map(Path, ('/etc/systemd/system', '/run/systemd/system',
    '/run/systemd/transient', '/usr/local/bin', '/etc/cron.d', '/var/spool/cron', '/etc/patternlab')))
ROOT_OWNER = 0
PROC_ROOT = Path('/proc')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def within(value, root):
    return value == str(root) or value.startswith(str(root)+'/')


def regular(path):
    require(not path.is_symlink() and path.is_file(), 'Expected a regular evidence file')
    return path


def inventory(root):
    require(root in TARGETS and not root.is_symlink() and root.is_dir(), 'Unreviewed cleanup target')
    for path in (root.parent, root):
        info = path.lstat()
        require(info.st_uid == ROOT_OWNER and not info.st_mode & 0o022,
                'Cleanup directory ownership/permissions changed')
    device = root.stat().st_dev
    entries, allocated, files = [], 0, []
    for path in (root, *sorted(root.rglob('*'))):
        info = path.lstat()
        require(not stat.S_ISLNK(info.st_mode), 'Symlink inside cleanup target')
        require(info.st_dev == device and (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)),
                'Mount or special file inside cleanup target')
        if stat.S_ISREG(info.st_mode):
            require(info.st_nlink == 1, 'Linked file inside cleanup target')
            files.append((path.relative_to(root).as_posix(), info.st_size))
        entries.append([path.relative_to(root).as_posix(), stat.S_IFMT(info.st_mode),
                        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink])
        allocated += info.st_blocks*512
    return dict(entries=entries, files=files, allocated_bytes=allocated)


def validate_evidence(root, tree):
    if root in STAGE_ANCHORS:
        extra_path = regular(root/'linux-bundle-manifest.json')
        require(digest(extra_path) == STAGE_ANCHORS[root], 'Staging anchor mismatch')
        extra = json.loads(extra_path.read_text())
        base_path = regular(root/'manifest.json')
        require(digest(base_path) == extra['recovery_manifest_sha256'], 'Recovery manifest mismatch')
        base = json.loads(base_path.read_text())
        require(base.get('state') == extra.get('state') == 'complete', 'Incomplete staging evidence')
        expected = {}
        for row in [*base['files'], *extra['files']]:
            name = row['path']
            path = PurePosixPath(name)
            require(path.as_posix() == name and not path.is_absolute() and '..' not in path.parts
                    and name not in expected, 'Invalid or duplicate inventory entry')
            expected[name] = row['bytes']
        actual = {name.removeprefix('files/'): size for name, size in tree['files'] if name.startswith('files/')}
        require(actual == expected, 'Staging payload differs from accepted inventory')
        require({name for name, _ in tree['files'] if not name.startswith('files/')}
                == {'manifest.json', 'linux-bundle-manifest.json', 'verification.json'},
                'Uninventoried file outside staging payload')
        evidence = ('manifest.json', 'linux-bundle-manifest.json', 'verification.json')
    else:
        marker = regular(root/'.linux-replay-sandbox.json')
        value = json.loads(marker.read_text())
        require(value.get('version') == 'patternlab-linux-frozen-replay-sandbox-v1'
                and value.get('disposable') is True and value.get('production') is False
                and value.get('project_root') == str(root), 'Replay is not the accepted disposable copy')
        evidence = ('.linux-replay-sandbox.json',)
    return {name: digest(regular(root/name)) for name in evidence}


def backup_evidence():
    require(digest(regular(FIRST_MANIFEST)) == FIRST_SHA, 'Initial backup manifest mismatch')
    require(json.loads(FIRST_MANIFEST.read_text()).get('state') == 'complete', 'Initial backup incomplete')
    require(digest(regular(DAILY_ARCHIVE)) == DAILY_SHA, 'Independent-copy backup archive mismatch')
    receipt = json.loads(regular(Path(str(DAILY_ARCHIVE)+'.verified.json')).read_text())
    require(receipt.get('state') == 'verified' and receipt.get('archive_sha256') == DAILY_SHA
            and receipt.get('point_name') == DAILY_ARCHIVE.name
            and receipt.get('databases_restored', 0) > 0, 'Daily backup restoration evidence missing')
    return dict(first_manifest_sha256=FIRST_SHA, daily_archive_sha256=DAILY_SHA,
                daily_archive_bytes=DAILY_ARCHIVE.stat().st_size)


def mounted_paths():
    def unescape(value):
        for code, char in (('\\040', ' '), ('\\011', '\t'), ('\\012', '\n'), ('\\134', '\\')):
            value = value.replace(code, char)
        return value
    return [unescape(line.split()[4]) for line in (PROC_ROOT/'self/mountinfo').read_text().splitlines()]


def active_references():
    hits = []
    for mounted in mounted_paths():
        if any(within(mounted, root) for root in TARGETS):
            hits.append(dict(kind='mount', path=mounted))
    for root in CONFIG_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob('*'):
            if not path.is_file() or path.is_symlink():
                continue
            require(path.stat().st_size <= 8*1024*1024, 'Oversized runtime configuration needs manual review')
            text = path.read_text(errors='replace')
            if any(str(target.parent) in text for target in TARGETS):
                hits.append(dict(kind='configured_reference', path=str(path)))
    for path in LIVE.rglob('*'):
        if path.is_symlink() and any(within(str(path.resolve()), root) for root in TARGETS):
            hits.append(dict(kind='live_symlink', path=str(path)))
    for proc in PROC_ROOT.iterdir():
        if not proc.name.isdigit():
            continue
        paths = []
        for link in (proc/'cwd', proc/'root', proc/'exe'):
            try:
                paths.append(os.readlink(link))
            except (FileNotFoundError, ProcessLookupError):
                pass
        try:
            descriptors = list((proc/'fd').iterdir())
        except (FileNotFoundError, ProcessLookupError):
            descriptors = []
        for link in descriptors:
            try:
                paths.append(os.readlink(link))
            except (FileNotFoundError, ProcessLookupError):
                pass
        try:
            paths.extend(line.split(None, 5)[5].rstrip().removesuffix(' (deleted)')
                         for line in (proc/'maps').read_text().splitlines() if len(line.split(None, 5)) == 6)
            command = (proc/'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
            if any(str(root) in command for root in TARGETS):
                hits.append(dict(kind='process_command', pid=proc.name))
        except (FileNotFoundError, ProcessLookupError):
            pass
        if any(within(path.removesuffix(' (deleted)'), root) for path in paths for root in TARGETS):
            hits.append(dict(kind='process_path', pid=proc.name))
    return hits


def protected_state():
    spec = importlib.util.spec_from_file_location('staging_cleanup_base',
        Path(__file__).with_name('install_adaptive_proposals.py'))
    base = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(base)
    base.FILES = ()
    base.SOURCES = ()
    frozen = base.frozen()
    paths = {str(path): digest(path) for path in LIVE.glob('*.py')}
    paths.update(base.protected_snapshot())
    for path in (LIVE/'data/api_football/scenario_policy/ENABLED',
                 LIVE/'data/api_football/scenario_policy/calibration.json'):
        paths[str(path)] = digest(regular(path))
    return dict(frozen=frozen, sha256=paths)


def audit():
    require(os.geteuid() == 0, 'Root privileges required for complete process checks')
    require(len(TARGETS) == len(set(TARGETS)) == 4, 'Unexpected cleanup scope')
    require(not any(LIVE == root or LIVE in root.parents or root in LIVE.parents for root in TARGETS),
            'Live application overlaps cleanup scope')
    targets = {}
    for root in TARGETS:
        tree = inventory(root)
        tree['evidence_sha256'] = validate_evidence(root, tree)
        targets[str(root)] = tree
    backups = backup_evidence()
    references = active_references()
    require(not references, 'Cleanup targets still have active/configured references: '+json.dumps(references))
    plan = dict(version='retire-initial-staging-v1', targets=targets,
                retained_backups=backups, protected=protected_state())
    return plan, fingerprint(plan)


def apply(expected):
    plan, actual = audit()
    require(actual == expected, 'Cleanup plan changed; repeat read-only audit')
    require(shutil.rmtree.avoids_symlink_attacks, 'Symlink-resistant deletion unavailable')
    require(not MAINTENANCE.is_symlink(), 'Maintenance root must not be a symlink')
    MAINTENANCE.mkdir(mode=0o700, parents=True, exist_ok=True)
    receipt_root = MAINTENANCE/('initial-staging-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:6])
    receipt_root.mkdir(mode=0o700)
    (receipt_root/'plan.json').write_text(json.dumps(plan, indent=2)+'\n')
    for root in TARGETS:
        folder = receipt_root/'evidence'/root.parent.name/root.name
        folder.mkdir(mode=0o700, parents=True)
        for name, expected_hash in plan['targets'][str(root)]['evidence_sha256'].items():
            destination = folder/name
            shutil.copy2(root/name, destination)
            require(digest(destination) == expected_hash, 'Retained evidence copy mismatch')
    require(not active_references(), 'Cleanup target became active before deletion')
    require(protected_state() == plan['protected'], 'Protected application changed before cleanup')
    before = shutil.disk_usage(LIVE).free
    removed = []
    try:
        for root in TARGETS:
            current = inventory(root)
            expected_tree = {key:value for key,value in plan['targets'][str(root)].items() if key != 'evidence_sha256'}
            require(current == expected_tree, 'Target changed before deletion')
            shutil.rmtree(root)
            removed.append(str(root))
            (receipt_root/'progress.json').write_text(json.dumps(dict(removed=removed))+'\n')
        require(protected_state() == plan['protected'], 'Protected application changed during cleanup')
        backups = backup_evidence()
        require(backups == plan['retained_backups'], 'Retained backup changed during cleanup')
        receipt = dict(state='initial_staging_retired', completed_at=datetime.now(timezone.utc).isoformat(),
            plan_sha256=actual, removed=removed, receipt_directory=str(receipt_root),
            removed_allocated_bytes=sum(row['allocated_bytes'] for row in plan['targets'].values()),
            free_bytes_before=before, free_bytes_after=shutil.disk_usage(LIVE).free,
            live_data_modified=False, backups_removed=False, cutover_removed=False,
            models_unchanged=True, authentication_unchanged=True, services_restarted=False,
            retained_backups=backups)
        (receipt_root/'receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')
        return receipt
    except BaseException:
        (receipt_root/'incomplete.json').write_text(json.dumps(dict(state='incomplete', removed=removed))+'\n')
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', metavar='AUDITED_PLAN_SHA256')
    args = parser.parse_args()
    if args.apply:
        print(json.dumps(apply(args.apply)))
    else:
        plan, plan_hash = audit()
        print(json.dumps(dict(state='read_only_audit', plan_sha256=plan_hash,
            targets={name:dict(files=len(row['files']), allocated_bytes=row['allocated_bytes'],
                              evidence_sha256=row['evidence_sha256']) for name,row in plan['targets'].items()},
            retained_backups=plan['retained_backups'], active_references=0,
            protected_sources=len(plan['protected']['sha256']))))
