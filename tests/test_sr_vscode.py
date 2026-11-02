import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sr_vscode', REPO / 'files/sr-vscode.py')
vscode = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vscode)


class ConfigTest(unittest.TestCase):
    def test_clangd_install_preserves_jsonc_and_custom_path(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            tree, home = base / 'tree', base / 'new-user'
            (tree / 'be/src').mkdir(parents=True)
            (tree / 'build.sh').touch()
            (tree / '.vscode').mkdir()
            settings = tree / '.vscode/settings.json'
            settings.write_text('{/* keep */ "editor.tabSize": 4,}\n')
            with patch.object(Path, 'home', return_value=home):
                vscode.install_clangd(tree, REPO)
                before = settings.read_text()
                self.assertIn('/* keep */', before)
                self.assertEqual(vscode.parse(before).value['clangd.path'], str(home / 'bin/clangd'))
                vscode.install_clangd(tree, REPO)
                self.assertEqual(before, settings.read_text())
                settings.write_text('{"clangd.path":"/custom/clangd"}')
                vscode.install_clangd(tree, REPO)
                self.assertEqual(settings.read_text(), '{"clangd.path":"/custom/clangd"}')

    def test_fe_config_merge(self):
        text = vscode.merge('{"version":"0.2.0", /* keep */}', '/farm/deploy')
        be = vscode.parse(text).value['configurations'][0]
        result = vscode.merge(text, '/farm/deploy', be=False, fe_port=28008)
        configs = vscode.parse(result).value['configurations']
        self.assertEqual(configs[0], be)
        self.assertEqual(configs[1]['type'], 'java')
        self.assertEqual(configs[1]['port'], 28008)
        self.assertIn('/* keep */', result)
        self.assertEqual(result, vscode.merge(result, '/farm/deploy', fe_port=28008))
        result = result.replace('"projectName": "fe-core"', '"projectName": "custom-project"')
        changed = vscode.merge(result, '/farm/deploy', be=False, fe_port=23078)
        fe = vscode.parse(changed).value['configurations'][1]
        self.assertEqual(fe['port'], 23078)
        self.assertEqual(fe['projectName'], 'custom-project')

    def test_fe_only_needs_no_lldb_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            tree = Path(temp)
            (tree / 'build.sh').touch()
            (tree / 'be/src').mkdir(parents=True)
            (tree / 'output/fe/lib').mkdir(parents=True)
            (tree / 'output/fe/lib/fe-core.jar').touch()
            with patch.object(vscode, 'install_adapter') as adapter:
                vscode.install(tree, REPO, '/farm/deploy', be=False, fe_port=28008)
                adapter.assert_not_called()
            config = vscode.parse((tree / '.vscode/launch.json').read_text()).value['configurations']
            self.assertEqual([c['name'] for c in config], [vscode.FE_NAME])

    def test_preserve_jsonc_and_idempotence(self):
        custom = '{"name":"mine", "url":"https://host/a,b", "args":["/*literal*/",],}'
        for comma in ('', ','):
            original = '{"version":"0.2.0", /* keep */ "configurations":[' + custom + comma + '// keep, too\n],}'
            result = vscode.merge(original, '/custom deploy')
            configs = vscode.parse(result).value['configurations']
            self.assertIn(custom, result)
            self.assertIn('// keep, too', result)
            self.assertEqual(configs[-1]['program'], '/custom deploy/be/lib/starrocks_be')
            self.assertEqual(configs[-1]['debugAdapterExecutable'], '${env:HOME}/bin/lldb-dap')
            self.assertEqual(result, vscode.merge(result, '/custom deploy'))

    def test_migrate_only_managed_fields(self):
        text = vscode.merge('{"version":"0.2.0"}', '/old')
        text = text.replace(vscode.ADAPTER, '/data/another-user/bin/lldb-dap')
        text = text.replace('"waitFor": false,', '"waitFor": true, "stopCommands": ["bt"], // custom\n')
        result = vscode.merge(text, '/new')
        config = vscode.parse(result).value['configurations'][0]
        self.assertEqual(config['debugAdapterExecutable'], vscode.ADAPTER)
        self.assertEqual(config['program'], '/new/be/lib/starrocks_be')
        self.assertTrue(config['waitFor'])
        self.assertEqual(config['stopCommands'], ['bt'])
        self.assertIn('// custom', result)

    def test_missing_fields_and_empty_arrays(self):
        for text in ('{}', '{"configurations":[]}', '{"configurations":[/* keep */]}'):
            result = vscode.merge(text, '/farm/deploy')
            self.assertEqual(len(vscode.parse(result).value['configurations']), 1)
            self.assertEqual(result, vscode.merge(result, '/farm/deploy'))
        text = '{"configurations":[{"name":' + vscode.encoded(vscode.NAME) + ',"type":"lldb-dap","request":"attach"}]}'
        result = vscode.merge(text, '/farm/deploy')
        self.assertEqual(vscode.parse(result).value['configurations'][0]['debugAdapterExecutable'], vscode.ADAPTER)

    def test_invalid_or_ambiguous_configuration(self):
        for text in ('{"configurations":', '{"configurations":{}}', '{"configurations":[0]}',
                     '{"configurations":[],"configurations":[]}'):
            with self.assertRaises(ValueError):
                vscode.merge(text, '/farm/deploy')

    def test_install_new_worktree_and_conflict(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            home = base / 'another user'
            tree = base / 'new worktree'
            (tree / 'be/src').mkdir(parents=True)
            (tree / 'build.sh').touch()
            binary = tree / 'output/be/lib/starrocks_be'
            binary.parent.mkdir(parents=True)
            binary.touch(mode=0o755)
            with patch.object(Path, 'home', return_value=home):
                vscode.install(tree / 'be/src', REPO, '/farm/deploy')
                launch = tree / '.vscode/launch.json'
                self.assertEqual((home / 'bin/lldb-dap').resolve(), REPO / 'files/lldb-host')
                before = launch.read_bytes()
                vscode.install(tree, REPO, '/farm/deploy')
                self.assertEqual(before, launch.read_bytes())
                self.assertNotIn(str(home), launch.read_text())
                (home / 'bin/lldb-dap').unlink()
                (home / 'bin/lldb-dap').write_text('user-owned command')
                with self.assertRaises(ValueError):
                    vscode.install(tree, REPO, '/other')
                self.assertEqual(before, launch.read_bytes())


class DispatchTest(unittest.TestCase):
    def test_deploy_dispatch(self):
        # Run the actual sr entry point with a Docker stand-in. No service starts.
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            repo = base / 'srdev'
            (repo / 'files').mkdir(parents=True)
            script = repo / 'sr'
            script.write_bytes((REPO / 'sr').read_bytes())
            script.chmod(0o755)
            (repo / 'Dockerfile').write_text('FROM unused-test-base\n')
            for name in ('clangd-host', 'lldb-host'):
                (repo / 'files' / name).touch()
            (repo / 'files/sr-image-id.py').write_bytes((REPO / 'files/sr-image-id.py').read_bytes())
            (repo / 'files/sr-vscode.py').write_text('import sys\nprint("GENERATOR", repr(sys.argv[1:]))\n')
            mockbin = base / 'bin'
            mockbin.mkdir()
            docker = mockbin / 'docker'
            docker.write_text('#!/bin/bash\nif [ "$1" = inspect ]; then echo true; exit 0; fi\n'
                              'case "$*" in *share/sr/image-id*) echo "$TEST_IMAGE_ID"; exit 0;; *.fe-debug-port*) echo 28008; exit 0;; esac\n'
                              'for a in "$@"; do [ "$a" = sr-deploy ] && exit "${TEST_DEPLOY_RC:-0}"; done\nexit 0\n')
            docker.chmod(0o755)
            env = dict(os.environ, PATH=str(mockbin) + os.pathsep + os.environ['PATH'],
                       BASE=str(base / 'base'), SR_TPDIR=str(base / 'tp'), SR_CONF=str(base / 'missing'),
                       SR_DEPLOY_DIR='/custom/deploy', SLICE='sr_vscode_test_missing')
            env['TEST_IMAGE_ID'] = subprocess.check_output(['python3', str(repo / 'files/sr-image-id.py'), str(repo), '', ''], text=True).strip()
            for args, rc, expected in [([], 0, True), (['--no-start'], 0, True),
                                       (['--be-only'], 0, True), (['--status'], 0, False),
                                       (['--stop'], 0, False), (['--fe-only'], 0, True),
                                       (['--help'], 0, False), ([], 7, False)]:
                with self.subTest(args=args, rc=rc):
                    run = subprocess.run(['bash', str(script), 'deploy', *args], cwd=base,
                                         env=dict(env, TEST_DEPLOY_RC=str(rc)), capture_output=True, text=True)
                    self.assertEqual(run.returncode, rc, run.stdout + run.stderr)
                    self.assertEqual('GENERATOR' in run.stdout, expected, run.stdout + run.stderr)
                    if expected:
                        self.assertIn('/custom/deploy', run.stdout)
                        self.assertEqual('--no-be' in run.stdout, '--fe-only' in args)
                        self.assertEqual('--fe-port' in run.stdout, '--be-only' not in args)


if __name__ == '__main__':
    unittest.main()
