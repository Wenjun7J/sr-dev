import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class ImageIdentityTest(unittest.TestCase):
    def test_identity_depends_on_build_inputs_not_checkout_path(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            a, b = base / 'a', base / 'b'
            a.mkdir()
            (a / 'Dockerfile').write_text('FROM base\nCOPY files/tool /usr/bin/tool\n')
            (a / 'sr').write_text('host-entry')
            (a / 'files').mkdir()
            (a / 'files/tool').write_text('v1\n')
            for name in ('clangd-host', 'lldb-host', 'sr-vscode.py', 'sr-image-id.py'):
                (a / 'files' / name).write_text('test')
            shutil.copytree(a, b)
            def identity(path, image='base'):
                return subprocess.check_output(['python3', str(REPO / 'files/sr-image-id.py'), str(path), image, ''], text=True)
            self.assertEqual(identity(a), identity(b))
            (b / 'files/srdev.conf.example').write_text('unrelated old file')
            self.assertEqual(identity(a), identity(b))
            self.assertNotEqual(identity(a), identity(b, 'another-base'))
            (b / 'files/tool').write_text('v2\n')
            self.assertNotEqual(identity(a), identity(b))

    def test_old_container_rejects_new_work_but_allows_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            mock = base / 'bin'
            mock.mkdir()
            docker = mock / 'docker'
            docker.write_text('#!/bin/bash\nif [ "$1" = inspect ]; then echo true; fi\nexit 0\n')
            docker.chmod(0o755)
            env = dict(os.environ, PATH=str(mock) + ':' + os.environ['PATH'],
                       BASE=str(base / 'base'), SR_TPDIR=str(base / 'tp'), SR_CONF=str(base / 'missing'))
            result = subprocess.run(['bash', str(REPO / 'sr'), 'deploy'], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('工具版本与当前 srdev 不一致', result.stderr)
            recovery = subprocess.run(['bash', str(REPO / 'sr'), 'deploy', '--stop'], env=env, capture_output=True, text=True)
            self.assertEqual(recovery.returncode, 0, recovery.stdout + recovery.stderr)


if __name__ == '__main__':
    unittest.main()
