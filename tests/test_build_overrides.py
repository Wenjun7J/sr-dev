"""Verify build decisions against conflicting site configuration in isolation."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
CONCURRENCY = ('SR_LOCAL_JOBS', 'SR_FE_UT_JOBS', 'SR_LINK_JOBS', 'SR_LINK_MEM_GB')


@unittest.skipUnless(sys.platform == 'linux' and os.geteuid() == 0
                     and os.environ.get('SR_TEST_NETNS') == 'isolated'
                     and Path('/.dockerenv').exists(), 'isolated root Linux container')
class BuildOverridesTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='sr-build-overrides-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.tree = self.root / 'tree'
        (self.tree / 'be/src').mkdir(parents=True)
        for name in ('build.sh', 'env.sh', 'run-be-ut.sh', 'run-fe-ut.sh'):
            (self.tree / name).write_text('#!/bin/sh\n# CMAKE_BUILD_PREFIX\nexit 91\n')
        self.conf = Path('/farm/srdev.conf')
        self.conf.parent.mkdir(exist_ok=True)
        previous = self.conf.read_bytes() if self.conf.exists() else None
        self.addCleanup(self.restore_conf, previous)
        self.configured = 'SR_LOCAL_JOBS=16\nSR_FE_UT_JOBS=8\nSR_LINK_JOBS=2\nSR_LINK_MEM_GB=15\n'
        self.conf.write_text(self.configured)
        mockbin = self.root / 'bin'
        mockbin.mkdir()
        # Keep daemon discovery on the "unusable existing daemon" path. The
        # test must never start iceccd, even inside its disposable container.
        pgrep = mockbin / 'pgrep'
        pgrep.write_text(f'#!/bin/sh\nprintf "%s\\n" {os.getpid()}\n')
        pgrep.chmod(0o755)
        self.env = dict(os.environ, PATH=str(mockbin) + os.pathsep + os.environ['PATH'],
                        SR_DRYRUN='1', FARM_STATE=str(self.root / 'missing-state'))
        for key in (*CONCURRENCY, 'SR_VERSION_KEY', 'SR_TPDIGEST_FULL', 'SR_TPDIGEST'):
            self.env.pop(key, None)

    def restore_conf(self, previous):
        if previous is None:
            self.conf.unlink(missing_ok=True)
        else:
            self.conf.write_bytes(previous)

    def dry_run(self, target, overrides):
        env = dict(self.env, **overrides)
        result = subprocess.run(
            ['bash', str(REPO / 'files/sr-build'), target, '--dist', 'off',
             '--no-dynlib', '--no-java-ext', '--no-ccache'],
            cwd=self.tree, env=env, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('SR_DRYRUN=1', result.stdout)
        self.assertEqual(self.conf.read_text(), self.configured)
        return result.stdout

    def test_be_override_changes_actual_parallelism_and_link_budget(self):
        output = self.dry_run('--be-ut', dict(SR_LOCAL_JOBS='6', SR_LINK_JOBS='1',
                                            SR_LINK_MEM_GB='7'))
        self.assertIn('并发: -j6 ', output)
        self.assertIn('全容器同时 1 个 ld', output)
        self.assertIn('最坏单个链接 7G', output)

    def test_unset_or_empty_overrides_keep_site_configuration(self):
        for supplied in ({}, dict.fromkeys(CONCURRENCY, '')):
            with self.subTest(supplied=supplied):
                output = self.dry_run('--be-ut', supplied)
                self.assertIn('并发: -j16 ', output)
                self.assertIn('全容器同时 2 个 ld', output)
                self.assertIn('最坏单个链接 15G', output)

    def test_fe_override_changes_maven_parallelism(self):
        output = self.dry_run('--fe-ut', dict(SR_LOCAL_JOBS='6', SR_FE_UT_JOBS='3'))
        self.assertIn('并发: maven -T 3', output)
        self.assertIn('并发: maven -T 8', self.dry_run('--fe-ut', {}))

    def test_invalid_fe_override_keeps_existing_fallback_to_local_jobs(self):
        output = self.dry_run('--fe-ut', dict(SR_LOCAL_JOBS='6', SR_FE_UT_JOBS='invalid'))
        self.assertIn('并发: maven -T 6', output)


if __name__ == '__main__':
    unittest.main()
