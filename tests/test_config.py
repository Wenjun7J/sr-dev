#!/usr/bin/env python3
"""Exercise current config sync and mount drift behavior using a fake Docker."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SOURCE = (Path(__file__).resolve().parents[1] / 'sr').read_text()
FUNCTIONS = (
    SOURCE[SOURCE.index('sync_conf(){'):SOURCE.index('# "64g"')]
    + SOURCE[SOURCE.index('check_mounts(){'):SOURCE.index('# 把某个官方镜像')]
)
MOCKS = r'''
running(){ return 0; }
check_container_version(){ return 0; }
resize_if_needed(){ return 0; }
docker(){
  printf '%s\n' "$*" >> "$DOCKER_LOG"
  printf '%s\n' "$MOUNT_INFO"
}
'''


@unittest.skipUnless(sys.platform == 'linux', 'requires GNU stat on Linux')
class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sr-config-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.conf = self.root / 'site.conf'
        self.conf.write_text('WORKERS=5\nITYPE=8c32g\n')
        for name in ('farm', 'tp', 'ccache'):
            (self.root / name).mkdir()
        self.dest = self.root / 'farm/srdev.conf'
        mounts = []
        for name in ('farm', 'tp', 'ccache'):
            st = (self.root / name).stat()
            mounts.append(f'/{name} {st.st_dev}:{st.st_ino}')
        self.env = dict(os.environ, BASE=str(self.root), CONF_FILE=str(self.conf),
                        NAME='test-container', TPDIR=str(self.root / 'tp'),
                        DOCKER_LOG=str(self.root / 'docker.log'),
                        MOUNT_INFO='\n'.join(mounts))

    def run_shell(self, command):
        return subprocess.run(['bash', '-uc', MOCKS + FUNCTIONS + '\n' + command],
                              env=self.env, text=True, capture_output=True, timeout=5)

    def test_editor_replacement_is_published_atomically_with_private_permissions(self):
        first = self.run_shell('ensure_container')
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        old = self.dest.stat().st_ino
        replacement = self.root / 'replacement'
        replacement.write_text('WORKERS=6\nITYPE=8c16g\n')
        replacement.replace(self.conf)
        second = self.run_shell('ensure_container')
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.dest.read_text(), replacement_text := 'WORKERS=6\nITYPE=8c16g\n')
        self.assertEqual(self.conf.read_text(), replacement_text)
        self.assertEqual(self.dest.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.dest.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotEqual(self.dest.stat().st_ino, old)

    def test_missing_source_keeps_last_snapshot_and_warns(self):
        self.assertEqual(self.run_shell('ensure_container').returncode, 0)
        before = self.dest.read_bytes()
        self.conf.unlink()
        run = self.run_shell('ensure_container')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.dest.read_bytes(), before)
        self.assertIn('上次同步', run.stderr)

    def test_copy_failure_preserves_previous_config_and_blocks_work(self):
        self.assertEqual(self.run_shell('ensure_container').returncode, 0)
        before = self.dest.read_bytes()
        self.conf.write_text('WORKERS=6\n')
        run = self.run_shell('cp(){ return 23; }; ensure_container')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('同步站点配置', run.stdout)
        self.assertEqual(self.dest.read_bytes(), before)
        self.assertEqual(list(self.dest.parent.glob('.srdev.conf.*')), [])

    def test_matching_mounts_are_accepted(self):
        self.assertEqual(self.run_shell('check_mounts').returncode, 0)

    def test_farm_drift_blocks_before_publishing_configuration(self):
        original = self.root / 'farm'
        original.rename(self.root / 'old-farm')
        original.mkdir()
        run = self.run_shell('ensure_container')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('/farm', run.stdout)
        self.assertFalse(self.dest.exists())
        calls = (self.root / 'docker.log').read_text().splitlines()
        self.assertTrue(all(line.startswith('exec ') for line in calls), calls)

    def test_thirdparty_drift_blocks_work(self):
        (self.root / 'tp').rename(self.root / 'old-tp')
        (self.root / 'tp').mkdir()
        run = self.run_shell('ensure_container')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('/tp', run.stdout)
        self.assertFalse(self.dest.exists())

    def test_cache_drift_warns_without_blocking_configuration(self):
        (self.root / 'ccache').rename(self.root / 'old-cache')
        (self.root / 'ccache').mkdir()
        run = self.run_shell('ensure_container')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('/ccache', run.stdout)
        self.assertEqual(self.dest.read_bytes(), self.conf.read_bytes())

    def test_mount_comparison_follows_host_symlink(self):
        (self.root / 'tp').rename(self.root / 'real-tp')
        (self.root / 'tp').symlink_to(self.root / 'real-tp', target_is_directory=True)
        run = self.run_shell('check_mounts')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == '__main__':
    unittest.main()
