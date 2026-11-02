"""Linux integration tests using disposable output trees, without real services."""
import fcntl
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == 'linux' and Path('/farm').is_dir(), 'run in a disposable Linux container with /farm')
class DeploySafetyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.mock = self.base / 'mock'
        self.mock.mkdir()
        self.env = dict(os.environ, SR_UID='1042', SR_PORT_BASE='28000',
                        SR_DEPLOY_DIR=str(self.base / 'deploy'),
                        PATH=str(self.mock) + ':' + os.environ['PATH'])
        self.exe(self.mock / 'pgrep', 'exit 1')
        self.exe(self.mock / 'hostname', 'echo 127.0.0.1')
        self.exe(self.mock / 'sleep', 'exit 0')
        self.exe(self.mock / 'ss', 'case "$*" in *":${TEST_OCCUPIED:-none}") echo "LISTEN 0 10 0.0.0.0:$TEST_OCCUPIED 0.0.0.0:*";; esac\nexit 0')

    def exe(self, path, body):
        path.write_text('#!/bin/bash\n' + body + '\n')
        path.chmod(0o755)

    def tree(self, name):
        root = self.base / name
        for d in ('be/src', 'output/be/lib', 'output/be/bin', 'output/be/conf'):
            (root / d).mkdir(parents=True)
        (root / 'build.sh').touch()
        (root / 'env.sh').touch()
        self.exe(root / 'output/be/lib/starrocks_be', 'exit 42')
        self.exe(root / 'output/be/bin/start_be.sh', 'echo expected-start-failure >&2; exit 42')
        (root / 'output/be/conf/be.conf').write_text('starlet_port = 9070\n')
        (root / 'output/be/lib/owner').write_text(name)
        return root

    def args(self, root, *args):
        return dict(args=['bash', str(REPO / 'files/sr-deploy'), '--be-only', *args], cwd=root,
                    env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def test_start_failure_is_failure(self):
        run = subprocess.run(**self.args(self.tree('failure')), timeout=10)
        self.assertNotEqual(run.returncode, 0, run.stdout)
        self.assertIn('expected-start-failure', run.stdout)
        self.assertNotIn('完成。连接', run.stdout)

    def test_fe_start_failure_without_firewall_changes(self):
        tree = self.tree('fe-failure')
        for d in ('lib', 'bin', 'conf'):
            (tree / 'output/fe' / d).mkdir(parents=True)
        (tree / 'output/fe/lib/fe.jar').touch()
        (tree / 'output/fe/conf/fe.conf').write_text('JAVA_OPTS="-Xmx8g"\n')
        self.exe(tree / 'output/fe/bin/start_fe.sh', 'echo expected-fe-failure >&2; exit 42')
        firewall_calls = self.base / 'firewall-calls'
        for command in ('sr-jdwp-guard', 'iptables', 'nft'):
            self.exe(self.mock / command, f'touch {firewall_calls}; exit 1')
        kwargs = self.args(tree)
        kwargs['args'] = ['bash', str(REPO / 'files/sr-deploy'), '--fe-only']
        failed = subprocess.run(**kwargs, timeout=5)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('expected-fe-failure', failed.stdout)
        stopped = subprocess.run(**self.args(tree, '--stop'), timeout=5)
        self.assertEqual(stopped.returncode, 0, stopped.stdout)
        self.assertFalse(firewall_calls.exists())

    def test_service_conflict_preserves_deployment(self):
        tree = self.tree('port-conflict')
        deployed = self.base / 'deploy/be/lib'
        deployed.mkdir(parents=True)
        (deployed / 'old').write_text('keep')
        self.env['TEST_OCCUPIED'] = '28006'
        run = subprocess.run(**self.args(tree, '--no-start'), timeout=10)
        self.assertNotEqual(run.returncode, 0, run.stdout)
        self.assertIn('28006', run.stdout)
        self.assertEqual((deployed / 'old').read_text(), 'keep')

    def test_shared_destination_lock_covers_other_tree_and_stop(self):
        a, b = self.tree('tree-A'), self.tree('tree-B')
        barrier = self.base / 'copied'
        release = self.base / 'release'
        self.exe(self.mock / 'cp', f'''/bin/cp "$@" || exit $?
case "$*" in *tree-A/output/be/lib*)
  touch {barrier}
  for i in $(seq 1 100); do [ -e {release} ] && exit 0; /bin/sleep 0.05; done
  exit 93;; esac''')
        first = subprocess.Popen(**self.args(a, '--no-start'))
        try:
            deadline = time.monotonic() + 4
            while not barrier.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(barrier.exists())
            for args in [('--no-start',), ('--stop',)]:
                second = subprocess.run(**self.args(b, *args), timeout=5)
                self.assertNotEqual(second.returncode, 0, second.stdout)
                self.assertIn('部署', second.stdout)
        finally:
            release.touch()
            output, _ = first.communicate(timeout=5)
        self.assertEqual(first.returncode, 0, output)
        self.assertEqual((self.base / 'deploy/be/lib/owner').read_text(), 'tree-A')

    def test_starlet_port_and_saved_base(self):
        tree = self.tree('ports')
        run = subprocess.run(**self.args(tree, '--no-start'), timeout=5)
        self.assertEqual(run.returncode, 0, run.stdout)
        conf = (self.base / 'deploy/be/conf/be.conf').read_text()
        self.assertIn('starlet_port = 28009', conf)
        self.env.pop('SR_PORT_BASE')
        self.env['SR_UID'] = '1342'
        run = subprocess.run(**self.args(tree, '--no-start'), timeout=5)
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertEqual((self.base / 'deploy/.port-base').read_text(), '28000\n')

    def test_new_user_automatically_avoids_occupied_block(self):
        tree = self.tree('automatic-ports')
        self.env.pop('SR_PORT_BASE')
        self.exe(self.mock / 'ss', 'if [ "$*" = "-H -ltn" ]; then echo "LISTEN 0 10 0.0.0.0:26846 0.0.0.0:*"; fi\nexit 0')
        run = subprocess.run(**self.args(tree, '--no-start'), timeout=5)
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertEqual((self.base / 'deploy/.port-base').read_text(), '24000\n')

    def test_rejected_build_keeps_previous_dependency(self):
        tree = self.tree('build-lock')
        key = hashlib.md5(str(tree).encode()).hexdigest()[:12]
        version_key = hashlib.md5((str(tree) + '@test').encode()).hexdigest()[:12]
        lockfile = Path('/farm') / ('sr-build.' + key + '.lock')
        record = Path('/farm/versions') / version_key
        record.parent.mkdir(exist_ok=True)
        record.write_text('main ' + 'a'*64 + '\n')
        self.addCleanup(record.unlink)
        with lockfile.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            run = subprocess.run(['bash', str(REPO / 'files/sr-build'), '--be', '--dist', 'off'], cwd=tree,
                                 env=dict(self.env, SR_VERSION_KEY=version_key, SR_VER='4.0', SR_TPDIGEST_FULL='b'*64),
                                 capture_output=True, text=True, timeout=5)
        self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('已有构建', run.stdout)
        self.assertEqual(record.read_text(), 'main ' + 'a'*64 + '\n')


if __name__ == '__main__':
    unittest.main()
