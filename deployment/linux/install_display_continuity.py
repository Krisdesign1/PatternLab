"""Source-only display pagination and reading continuity, with immutable archive checks.

Reuse the verified presentation rollout's source rollback, service recovery,
frozen-model/authentication checks and append-only live-record comparison.
No database copy, policy migration, training or timer mutation is performed.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tarfile


spec = importlib.util.spec_from_file_location('display_source_installer',
    Path(__file__).with_name('install_table_views.py'))
views = importlib.util.module_from_spec(spec)
spec.loader.exec_module(views)
base = views.base
require = views.require

SOURCES = (
    'scenario_catalog_panel.py', 'scenario_workspace_panel.py', 'calendar_panel.py',
    'journal_panel.py', 'daily_analysis_panel.py', 'high_probability_panel.py',
)


def load_bundle(path, expected):
    require(base.digest(path) == expected, 'Bundle hash mismatch')
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        require(all(item.isfile() for item in members), 'Only regular files allowed')
        require(len({item.name for item in members}) == len(members), 'Duplicate member')
        require({item.name for item in members} == set(SOURCES) | {'manifest.json'}, 'Unexpected source scope')
        manifest = json.load(archive.extractfile('manifest.json'))
        require(set(manifest['sha256']) == set(SOURCES) == set(manifest['previous_sha256']),
                'Incomplete reviewed source manifest')
        payload = {name: archive.extractfile(name).read() for name in SOURCES}
    for name, data in payload.items():
        require(hashlib.sha256(data).hexdigest() == manifest['sha256'][name], 'Payload hash mismatch')
        compile(data, name, 'exec')
        target = base.ROOT / name
        require(not target.is_symlink(), 'Unexpected target symlink')
        require((base.digest(target) if target.exists() else None) == manifest['previous_sha256'][name],
                'Live source differs from reviewed baseline: ' + name)
    return manifest, payload


def deploy(bundle, expected):
    previous = views.load_bundle
    try:
        views.load_bundle = load_bundle
        return views.deploy(bundle, expected, backup_prefix='display-continuity',
            receipt_state='display_continuity_deployed',
            storage='source_only_no_archive_rewrite')
    finally:
        views.load_bundle = previous


if __name__ == '__main__':
    require(len(sys.argv) == 3, 'Usage: install_display_continuity.py BUNDLE SHA256')
    print(json.dumps(deploy(sys.argv[1], sys.argv[2])))
