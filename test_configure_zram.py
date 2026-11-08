import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from deployment.linux import configure_zram as memory


class CompressedMemoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sys = self.root/'device'; self.sys.mkdir()
        for name, value in dict(disksize='0', comp_algorithm='lzo-rle [lzo] zstd',
                                backing_dev='none', mem_limit='0').items():
            (self.sys/name).write_text(value)
        self.marker = self.root/'owned.json'
        self.swaps = self.root/'swaps'; self.swaps.write_text('Filename Type Size Used Priority\n')
        self.runner = Mock()

    def run_setup(self):
        return memory.setup(sys_path=self.sys, marker=self.marker, swaps_path=self.swaps, runner=self.runner)

    def test_new_empty_device_uses_ram_only_and_records_private_ownership(self):
        value = self.run_setup()
        self.assertEqual(value['state'], 'active')
        self.assertFalse(value['disk_writeback'])
        self.assertEqual((self.sys/'disksize').read_text(), str(memory.LOGICAL))
        self.assertEqual((self.sys/'mem_limit').read_text(), str(memory.PHYSICAL))
        self.assertEqual(self.marker.stat().st_mode & 0o777, 0o600)
        self.assertEqual([call.args[0][0] for call in self.runner.call_args_list],
                         ['/usr/sbin/mkswap', '/usr/sbin/swapon'])

    def test_unowned_active_or_initialized_device_is_never_overwritten(self):
        for active in (False, True):
            with self.subTest(active=active):
                self.swaps.write_text('Filename Type Size Used Priority\n' + ('/dev/zram0 partition 1 0 100\n' if active else ''))
                (self.sys/'disksize').write_text('123')
                with self.assertRaises(ValueError):
                    self.run_setup()
                self.assertEqual((self.sys/'disksize').read_text(), '123')
        self.runner.assert_not_called()

    def test_owned_active_device_is_idempotent_without_reset_or_mkswap(self):
        self.run_setup()
        (self.sys/'comp_algorithm').write_text('lzo [zstd]')
        (self.sys/'mm_stat').write_text(f'4096 58 20480 {memory.PHYSICAL} 20480 0 0 0 0')
        # The real kernel's mem_limit attribute is write-only. Idempotence
        # reads the exposed physical limit from mm_stat instead.
        (self.sys/'mem_limit').unlink()
        for name in ('/dev/zram0', '/zram0'):
            with self.subTest(name=name):
                self.swaps.write_text('Filename Type Size Used Priority\n'+name+' partition 4194304 10 100\n')
                self.runner.reset_mock()
                self.assertEqual(self.run_setup()['state'], 'active')
                self.runner.assert_not_called()

    def test_disk_backing_or_missing_algorithm_is_refused_without_device_write(self):
        for name, value in [('backing_dev', 'sda1'), ('comp_algorithm', '[lzo]')]:
            before = (self.sys/name).read_text()
            (self.sys/name).write_text(value)
            with self.assertRaises(ValueError):
                self.run_setup()
            self.assertEqual((self.sys/'disksize').read_text(), '0')
            (self.sys/name).write_text(before)
        self.runner.assert_not_called()

    def test_failed_swap_activation_does_not_claim_ownership_or_success(self):
        self.runner.side_effect = [None, RuntimeError('failure')]
        with self.assertRaises(RuntimeError):
            self.run_setup()
        self.assertFalse(self.marker.exists())


if __name__ == '__main__':
    unittest.main()
