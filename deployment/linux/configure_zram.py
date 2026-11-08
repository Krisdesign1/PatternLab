"""Provision only PatternLab's dedicated compressed-RAM swap device.

No disk backing, package installation, service restart or model/data access.
Existing devices without our private ownership marker are never overwritten.
Stopping the unit deliberately does not swapoff under memory pressure.
"""
import json
import os
from pathlib import Path
import subprocess

LOGICAL = 4 * 1024**3
PHYSICAL = 1536 * 1024**2
DEVICE = '/dev/zram0'
MARKER = Path('/var/lib/patternlab-ops/zram-owned.json')
SYS = Path('/sys/block/zram0')


def setup(*, sys_path=SYS, marker=MARKER, swaps_path=Path('/proc/swaps'), runner=subprocess.run):
    def command(*args):
        runner(list(args), check=True, capture_output=True, text=True, timeout=15)

    if marker.is_symlink() or marker.parent.is_symlink():
        raise ValueError('Ownership marker must not be a link.')
    owned = False
    if marker.exists():
        info = marker.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError('Invalid ownership marker.')
        owned = json.loads(marker.read_text()) == dict(device=DEVICE, logical_bytes=LOGICAL,
                                                       physical_limit_bytes=PHYSICAL, algorithm='zstd')
        if not owned:
            raise ValueError('Different device configuration already recorded.')
    swap_names = {line.split()[0] for line in swaps_path.read_text().splitlines()[1:] if line.split()}
    # Recent kernels may expose /zram0 rather than /dev/zram0 in /proc/swaps.
    active = bool(swap_names & {DEVICE, '/zram0'})
    if active and not owned:
        raise ValueError('An existing swap device is not owned by PatternLab.')
    if not sys_path.exists():
        command('/usr/sbin/modprobe', 'zram', 'num_devices=1')
    size = int((sys_path/'disksize').read_text())
    algorithms = (sys_path/'comp_algorithm').read_text().split()
    if 'zstd' not in {value.strip('[]') for value in algorithms}:
        raise ValueError('zstd compression is unavailable.')
    if (sys_path/'backing_dev').read_text().strip() != 'none':
        raise ValueError('Disk writeback must remain disabled.')
    if size:
        if (not owned or size != LOGICAL or '[zstd]' not in algorithms
                or int((sys_path/'mm_stat').read_text().split()[3]) != PHYSICAL):
            raise ValueError('An initialized device must not be overwritten.')
    else:
        (sys_path/'comp_algorithm').write_text('zstd')
        (sys_path/'mem_limit').write_text(str(PHYSICAL))
        (sys_path/'disksize').write_text(str(LOGICAL))
        command('/usr/sbin/mkswap', DEVICE)
    if not active:
        command('/usr/sbin/swapon', '--priority', '100', DEVICE)
    if not owned:
        with marker.open('x') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(dict(device=DEVICE, logical_bytes=LOGICAL, physical_limit_bytes=PHYSICAL,
                           algorithm='zstd'), stream)
            stream.flush(); os.fsync(stream.fileno())
    return dict(state='active', logical_bytes=LOGICAL, physical_limit_bytes=PHYSICAL,
                algorithm='zstd', disk_writeback=False)


if __name__ == '__main__':
    try:
        if os.geteuid() != 0:
            raise PermissionError()
        print(json.dumps(setup()))
    except Exception:
        print('{"state":"error","reason":"Compressed RAM swap configuration refused."}')
        raise SystemExit(1)
