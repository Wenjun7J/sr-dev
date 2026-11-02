import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('SR_TEST_NETNS') == 'isolated' and Path('/.dockerenv').exists(),
                     'requires explicit isolated Docker network namespace')
class DeployLockTest(unittest.TestCase):
    def test_deploy_lock_is_shared_across_uids_and_released(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ready = root / 'ready'
            script = root / 'hold.sh'
            script.write_text(f'touch {ready}\nexec /bin/sleep 30\n')
            helper = ['python3', str(REPO / 'files/sr-deploy-lock.py')]
            holder = subprocess.Popen(helper + [str(script)])
            def drop():
                os.setgroups([]); os.setgid(12345); os.setuid(12345)
            try:
                deadline = time.monotonic() + 3
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                blocked = subprocess.run(helper + ['/dev/null'], preexec_fn=drop, capture_output=True)
                self.assertEqual(blocked.returncode, 75)
            finally:
                holder.terminate(); holder.wait(timeout=3)
            allowed = subprocess.run(helper + ['/dev/null'], preexec_fn=drop, capture_output=True)
            self.assertEqual(allowed.returncode, 0, allowed.stderr)


if __name__ == '__main__':
    unittest.main()
