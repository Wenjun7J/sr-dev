"""Deletion and concurrency tests use synthetic caches, never the live /tp."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('tp_prune', REPO / 'files/sr-tp-prune.py')
tp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tp)
REPOSITORY = 'registry.example.invalid/dev-env'
A, B, C = 'a' * 64, 'b' * 64, 'c' * 64


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sr-tp-prune-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'tp'
        self.root.mkdir()
        self.farm = self.base / 'user/farm'
        (self.farm / 'versions').mkdir(parents=True)
        self.cache(A)
        self.cache(B)
        self.containers = []
        # Account homes belong to the host, not this isolated test fixture.
        mock = patch('pwd.getpwall', return_value=[])
        mock.start()
        self.addCleanup(mock.stop)

    def cache(self, dg, tag='latest', repo=REPOSITORY):
        directory = self.root / dg
        (directory / 'installed/lib').mkdir(parents=True)
        (directory / 'installed/lib/fake.a').write_bytes(b'synthetic-library')
        for key, value in {'.ready': '', '.image': f'{repo}:{tag}', '.gccmajor': '14',
                           '.digest': f'sha256:{dg}', '.ref': f'{repo}@sha256:{dg}'}.items():
            (directory / key).write_text(value + '\n')
        return directory

    def run_prune(self, apply=False, lookup=lambda image: B):
        with redirect_stdout(io.StringIO()):
            return tp.prune(self.root, self.farm, {REPOSITORY}, apply,
                            inventory=lambda: self.containers, lookup=lookup)

    def container(self, farm, label='1', running=True):
        return {'Name': '/synthetic_farm', 'Config': {'Labels': {tp.LABEL: label}},
                'State': {'Running': running},
                'Mounts': [{'Destination': '/tp', 'Source': str(self.root)},
                           {'Destination': '/farm', 'Source': str(farm)}]}

    def test_preview_does_not_delete_and_apply_removes_only_old(self):
        self.assertEqual(self.run_prune(), [A])
        self.assertTrue((self.root / A).exists())
        self.assertEqual(self.run_prune(True), [A])
        self.assertFalse((self.root / A).exists())
        self.assertTrue((self.root / B / '.ready').exists())

    def test_short_and_full_version_pins_survive(self):
        for dg in (A[:12], A):
            (self.farm / 'versions/tree').write_text(f'main {dg}\n')
            self.assertEqual(self.run_prune(True), [])

    def test_short_and_full_run_sidecars_survive_even_when_stale(self):
        for dg in (A[:12], A):
            (self.farm / 'sr-build.test.run').write_text(f'date\tuser\ttree\tmain/{dg}\tlocal\n')
            self.assertEqual(self.run_prune(True), [])

    def test_other_users_container_references_are_preserved(self):
        farm = self.base / 'another/farm'
        (farm / 'versions').mkdir(parents=True)
        (farm / 'versions/tree').write_text(f'main {A}\n')
        self.containers = [self.container(farm)]
        self.assertEqual(self.run_prune(True), [])

    def test_custom_base_registration_survives_container_removal(self):
        farm = self.base / 'custom-base/farm'
        (farm / 'versions').mkdir(parents=True)
        (farm / 'versions/tree').write_text(f'main {A}\n')
        tp.register(self.root, farm)
        tp.register(self.root, farm)
        self.assertEqual(self.run_prune(True), [])

    def test_concurrent_registration_is_atomic(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: tp.register(self.root, self.farm), range(32)))
        records = list(self.root.glob(tp.REGISTRATION + '*'))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].read_text().strip(), str(self.farm))
        self.assertEqual(list(self.root.glob('.sr-tp-register-*')), [])

    def test_symlinked_farm_can_be_registered(self):
        actual = self.base / 'state'
        (actual / 'versions').mkdir(parents=True)
        (actual / 'versions/tree').write_text(f'main {A}\n')
        alias = self.base / 'custom/farm'
        alias.parent.mkdir()
        alias.symlink_to(actual, target_is_directory=True)
        run = subprocess.run([sys.executable, str(REPO / 'files/sr-tp-prune.py'), 'register',
                              '--root', str(self.root), '--farm', str(alias)],
                             capture_output=True, text=True, timeout=5)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.run_prune(True), [])

    def test_default_account_home_reference_is_found(self):
        from types import SimpleNamespace
        with patch('pwd.getpwall', return_value=[SimpleNamespace(pw_dir=str(self.farm.parent))]):
            (self.farm / 'versions/tree').write_text(f'main {A}\n')
            self.assertEqual(self.run_prune(True), [])

    def test_legacy_running_and_stopped_containers_block_apply(self):
        for running in (True, False):
            self.containers = [self.container(self.farm, label='', running=running)]
            self.assertEqual(self.run_prune(), [A])
            with self.assertRaisesRegex(RuntimeError, '尚不支持依赖锁'):
                self.run_prune(True)
            self.assertTrue((self.root / A).exists())

    def test_unknown_farm_mount_aborts(self):
        container = self.container(self.farm)
        container['Mounts'].pop()
        self.containers = [container]
        with self.assertRaisesRegex(RuntimeError, '/farm'):
            self.run_prune(True)

    def test_all_source_tags_are_checked_including_pinned_entries(self):
        self.cache(C, tag='stable')
        (self.farm / 'versions/tree').write_text(f'stable {C}\n')
        self.assertEqual(self.run_prune(True, lambda image: A if image.endswith(':stable') else B), [])

    def test_registry_failure_aborts_before_any_deletion(self):
        def fail(image):
            raise OSError('registry unavailable')
        with self.assertRaises(OSError):
            self.run_prune(True, fail)
        self.assertTrue((self.root / A).exists())

    def test_deleted_source_tag_is_treated_as_old_cache(self):
        (self.farm / 'versions/tree').write_text(f'main {B}\n')
        self.assertEqual(self.run_prune(True, lambda image: None), [A])
        self.assertFalse((self.root / A).exists())
        self.assertTrue((self.root / B).exists())

    def test_current_digest_verifies_404_with_tags_list(self):
        from urllib.error import HTTPError
        import email.message

        def make_response(status, headers_dict=None, body=b''):
            class Resp:
                def __init__(self):
                    self.status = status
                    self.headers = email.message.Message()
                    for k, v in (headers_dict or {}).items():
                        self.headers[k] = v

                def read(self):
                    return body

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

            return Resp()

        responses = []

        class FakeOpener:
            def open(self, req, timeout=10):
                action = responses.pop(0)
                if isinstance(action, Exception):
                    raise action
                return action

        with patch('urllib.request.build_opener', return_value=FakeOpener()):
            err404 = HTTPError('http://r/v2/dev-env/manifests/old', 404, 'Not Found', {}, io.BytesIO())
            try:
                responses.extend([
                    err404,
                    make_response(200, body=json.dumps({'name': 'dev-env', 'tags': ['latest']}).encode()),
                ])
                self.assertIsNone(tp.current_digest(f'{REPOSITORY}:old'))
            finally:
                err404.close()

            for tag_action in [
                HTTPError('http://r/v2/dev-env/tags/list', 404, 'Not Found', {}, io.BytesIO()),
                HTTPError('http://r/v2/dev-env/tags/list', 501, 'Not Implemented', {}, io.BytesIO()),
                make_response(200, body=json.dumps({'name': 'dev-env', 'tags': []}).encode()),
                make_response(200, body=json.dumps({'name': 'dev-env', 'tags': ['old']}).encode()),
            ]:
                head_err = HTTPError('http://r/v2/dev-env/manifests/old', 404, 'Not Found', {}, io.BytesIO())
                try:
                    responses.clear()
                    responses.extend([head_err, tag_action])
                    with self.assertRaises(HTTPError):
                        tp.current_digest(f'{REPOSITORY}:old')
                finally:
                    head_err.close()
                    if isinstance(tag_action, HTTPError):
                        tag_action.close()

    def test_preview_skips_unreadable_reference_without_sudo(self):
        (self.farm / 'versions/tree').write_text(f'main {A}\n')
        original = tp.read_text

        def read(path):
            if path.name == 'tree':
                raise PermissionError('not readable')
            return original(path)

        with patch.object(tp, 'read_text', side_effect=read):
            self.assertEqual(self.run_prune(), [A])
        self.assertTrue((self.root / A).exists())

    def test_preview_skips_unreadable_registered_farm_and_container_mount(self):
        unreadable_farm = self.base / 'private-user/farm'
        reg_file = self.root / (tp.REGISTRATION + 'other')
        reg_file.write_text(str(unreadable_farm) + '\n')
        other_tp = self.base / 'private-user/thirdparty'
        other_farm = self.base / 'private-user/other-farm'
        self.containers = [{
            'Name': '/other_farm',
            'Config': {'Labels': {tp.LABEL: '1'}},
            'State': {'Running': True},
            'Mounts': [{'Destination': '/tp', 'Source': str(other_tp)},
                       {'Destination': '/farm', 'Source': str(other_farm)}],
        }, self.container(other_farm)]
        orig_stat = Path.stat
        orig_resolve = Path.resolve

        def mock_stat(p, *args, **kwargs):
            if p in (unreadable_farm, other_farm):
                raise PermissionError('denied')
            return orig_stat(p, *args, **kwargs)

        def mock_resolve(p, *args, **kwargs):
            if p in (other_tp, other_farm):
                raise PermissionError('denied')
            return orig_resolve(p, *args, **kwargs)

        with patch.object(Path, 'stat', mock_stat), patch.object(Path, 'resolve', mock_resolve):
            self.assertEqual(self.run_prune(False), [A])
            with self.assertRaises(PermissionError):
                self.run_prune(True)

    def test_stale_registration_with_file_ancestor_is_skipped(self):
        file_ancestor = self.base / 'now-a-file'
        file_ancestor.write_text('not a directory')
        reg_file = self.root / (tp.REGISTRATION + 'stale')
        reg_file.write_text(str(file_ancestor / 'farm') + '\n')
        self.assertEqual(self.run_prune(True), [A])

    def test_pure_fe_run_sidecar_does_not_abort_prune(self):
        (self.farm / 'sr-build.fe.run').write_text(
            '11-22 21:00:00\tuser\ttree@main\t-/-\t本地\t?\tfe-ut\t123\t/tmp/o\t/tmp/a\t编译\n'
        )
        self.assertEqual(self.run_prune(True), [A])

    def test_corrupt_reference_aborts(self):
        (self.farm / 'versions/tree').write_text('main ?\n')
        with self.assertRaisesRegex(RuntimeError, '版本记录'):
            self.run_prune(True)
        self.assertTrue((self.root / A).exists())

    def test_failed_reference_read_aborts(self):
        (self.farm / 'versions/tree').write_text(f'main {A}\n')
        original = tp.read_text
        def read(path):
            if path.name == 'tree':
                raise PermissionError('not readable')
            return original(path)
        with patch.object(tp, 'read_text', side_effect=read), self.assertRaises(PermissionError):
            self.run_prune(True)
        self.assertTrue((self.root / A).exists())

    def test_incomplete_unknown_and_temporary_directories_survive(self):
        (self.root / A / '.ready').unlink()
        self.cache(C, repo='unknown.example.invalid/dev-env')
        for name in ('other', A[:12], A + '.tmp.123', A + '.stale.123'):
            (self.root / name).mkdir()
        self.assertEqual(self.run_prune(True), [])
        self.assertEqual(len(list(self.root.iterdir())), 7)

    def test_mismatched_digest_metadata_survives(self):
        (self.root / A / '.digest').write_text('sha256:' + C)
        self.assertEqual(self.run_prune(True), [])

    def test_symlinked_metadata_or_digest_survives(self):
        (self.root / A / '.ready').unlink()
        (self.root / A / '.ready').symlink_to(self.root / B / '.ready')
        (self.root / C).symlink_to(self.root / B, target_is_directory=True)
        self.assertEqual(self.run_prune(True), [])

    def test_internal_symlinks_do_not_delete_external_files(self):
        outside = self.base / 'keep'
        outside.write_text('do not delete')
        (self.root / A / 'installed/link').symlink_to(outside)
        self.run_prune(True)
        self.assertEqual(outside.read_text(), 'do not delete')

    def test_reader_blocks_apply_but_not_preview(self):
        with tp.cache_lock(self.root):
            self.assertEqual(self.run_prune(), [A])
            with self.assertRaisesRegex(RuntimeError, '正在被'):
                self.run_prune(True)
        self.assertEqual(self.run_prune(True), [A])

    def test_exclusive_lock_covers_scan_through_delete(self):
        def lookup(image):
            # A late starter cannot acquire a read lease after the scan began.
            with self.assertRaises(RuntimeError):
                with tp.cache_lock(self.root):
                    self.fail('reader entered during prune')
            return B
        self.run_prune(True, lookup)
        self.assertFalse((self.root / A).exists())

    def test_metadata_change_before_delete_aborts(self):
        def lookup(image):
            (self.root / A / '.ref').write_text('unexpected replacement')
            return B
        with self.assertRaisesRegex(RuntimeError, '元数据发生变化'):
            self.run_prune(True, lookup)
        self.assertTrue((self.root / A).exists())

    @unittest.skipUnless(sys.platform == 'linux' and os.geteuid() == 0
                         and os.environ.get('SR_TEST_NETNS') == 'isolated', 'isolated root container')
    def test_directory_lock_is_shared_across_users(self):
        self.base.chmod(0o755)
        def drop():
            os.setgroups([])
            os.setgid(12345)
            os.setuid(12345)
        code = '''import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location('tp', sys.argv[1])
tp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tp)
try:
    with tp.cache_lock(pathlib.Path(sys.argv[2]), exclusive=True):
        pass
except RuntimeError:
    sys.exit(75)
'''
        command = [sys.executable, '-c', code, str(REPO / 'files/sr-tp-prune.py'), str(self.root)]
        with tp.cache_lock(self.root):
            blocked = subprocess.run(command, preexec_fn=drop, capture_output=True, timeout=5)
            self.assertEqual(blocked.returncode, 75, blocked.stderr)
        allowed = subprocess.run(command, preexec_fn=drop, capture_output=True, timeout=5)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    @unittest.skipUnless(sys.platform == 'linux', 'GNU flock directory locks')
    def test_build_child_retains_lease_after_shell_parent_exits(self):
        process = subprocess.Popen(['bash', '-c',
            'exec 6<"$1"; flock -s 6; exec 5<&0; (read -r line <&5) & echo ready', '_', str(self.root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            process.wait(timeout=5)
            with self.assertRaises(RuntimeError):
                self.run_prune(True)
        finally:
            process.communicate('\n', timeout=5)


class CliTests(unittest.TestCase):
    def test_tp_help_and_invalid_options_do_not_invoke_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = dict(os.environ, BASE=str(root / 'base'), SR_TPDIR=str(root / 'tp'),
                       SR_CONF=str(root / 'missing'), SR_REGISTRIES=str(root / 'registries'))
            (root / 'registries').write_text(f'ee {REPOSITORY}\n')
            for args, code, expected in [(['tp', '--help'], 0, '--yes'),
                                         (['tp', 'prune', '--help'], 0, '--yes'),
                                         (['tp', 'typo'], 2, '未知'),
                                         (['tp', 'prune', '--root', '/'], 2, '未知'),
                                         (['tp', 'prune', '--yes', '--dry-run'], 2, 'not allowed'),
                                         (['-v'], 2, '需要一个版本参数'),
                                         (['pack', '--name'], 2, '需要一个包名'),
                                         (['pack', '--out'], 2, '需要一个目录'),
                                         (['build', '--help'], 0, '用法: sr-build'),
                                         (['be-ut', '--help'], 0, 'run-be-ut.sh'),
                                         (['build', '--why'], 0, '实测依据')]:
                with self.subTest(args=args):
                    run = subprocess.run(['bash', str(REPO / 'sr'), *args], env=env,
                                         capture_output=True, text=True)
                    self.assertEqual(run.returncode, code, run.stdout + run.stderr)
                    self.assertIn(expected, run.stdout + run.stderr)
                    self.assertFalse((root / 'base/farm').exists())


@unittest.skipUnless(os.environ.get('SR_TEST_NETNS') == 'isolated', 'requires isolated Linux test network')
class RegistryIntegrationTests(unittest.TestCase):
    def test_cli_resolves_registry_and_deletes_only_old_cache(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        response = {'status': 200, 'digest': B}

        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(self):
                if self.path.endswith('/manifests/old'):
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(response['status'])
                self.send_header('Docker-Content-Digest', 'sha256:' + response['digest'])
                self.end_headers()

            def do_GET(self):
                if self.path.endswith('/tags/list') and response.get('tags_body') is not None:
                    payload = json.dumps(response['tags_body']).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_response(501)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                cache = root / 'tp'
                farm = root / 'farm'
                cache.mkdir()
                farm.mkdir()
                repo = f'127.0.0.1:{server.server_port}/dev-env'
                for dg, tag in ((A, 'old'), (B, 'latest')):
                    entry = cache / dg
                    (entry / 'installed').mkdir(parents=True)
                    for name, data in {'.ready': '', '.gccmajor': '14', '.image': repo + ':' + tag,
                                       '.ref': repo + '@sha256:' + dg, '.digest': 'sha256:' + dg}.items():
                        (entry / name).write_text(data)
                mockbin = root / 'bin'
                mockbin.mkdir()
                docker = mockbin / 'docker'
                docker.write_text('#!/bin/sh\n[ "$1" = ps ] && exit 0\nexit 1\n')
                docker.chmod(0o755)
                registries = root / 'registries'
                registries.write_text('ee ' + repo + '\n')
                env = dict(os.environ, PATH=str(mockbin) + ':' + os.environ['PATH'],
                           BASE=str(root), SR_TPDIR=str(cache), SR_CONF=str(root / 'missing'),
                           SR_REGISTRIES=str(registries), SR_TP_REPO='')
                command = ['bash', str(REPO / 'sr'), 'tp', 'prune']
                # When HEAD :old returns 404 and tags/list fails (501), abort without deleting anything.
                for status, digest in ((503, B), (404, B), (200, 'invalid')):
                    response.update(status=status, digest=digest, tags_body=None)
                    failed = subprocess.run(command + ['--yes'], env=env, text=True,
                                            capture_output=True, timeout=15)
                    self.assertNotEqual(failed.returncode, 0, failed.stdout)
                    self.assertTrue((cache / A).exists())
                    self.assertTrue((cache / B).exists())
                # When tags/list confirms repo is live and :old is deleted, prune removes A and keeps B.
                response.update(status=200, digest=B, tags_body={'name': 'dev-env', 'tags': ['latest']})
                for args in ([], ['--yes']):
                    run = subprocess.run(command + args, env=env, text=True,
                                         capture_output=True, timeout=15)
                    self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                    self.assertEqual((cache / A).exists(), not args)
                    self.assertTrue((cache / B).exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
