"""Regression tests for review findings and multi-user edge cases."""
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class CcachePchSedTests(unittest.TestCase):
    def test_prefix_map_escaping_and_last_equals_split(self):
        source = (REPO / 'files/ccache-pch').read_text()
        start = source.index('FMAPS=(); MIXED=0')
        end = source.index('hashed=()', start)
        snippet = source[start:end]
        script = (
            'set -euo pipefail\n'
            'out="$1"; args=("-ffile-prefix-map=$2")\n'
            + snippet +
            '\nprintf "%s\\n" "$3" | sed -f "$sedf"\nrm -f "$sedf"\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            old_dir = r'/tmp/a=1.[b]*^$|\dir'
            new_dir = r'rel&dir|x\y'
            line = f'# 1 "{old_dir}/foo.h"'
            run = subprocess.run(
                ['bash', '-c', script, '_', str(Path(tmp) / 'cmake_pch.hxx.gch'),
                 f'{old_dir}={new_dir}', line],
                capture_output=True, text=True, check=True,
            )
            self.assertEqual(run.stdout.rstrip('\n'), f'# 1 "{new_dir}/foo.h"')


class SrCliAndDispatchTests(unittest.TestCase):
    def _setup_mock_repo(self, base):
        repo = base / 'srdev'
        (repo / 'files').mkdir(parents=True)
        for name in ('sr',):
            dst = repo / name
            dst.write_bytes((REPO / name).read_bytes())
            dst.chmod(0o755)
        (repo / 'Dockerfile').write_text('FROM unused-base\n')
        for name in ('clangd-host', 'lldb-host', 'sr-vscode.py'):
            (repo / 'files' / name).write_text('#!/bin/sh\nexit 0\n')
            (repo / 'files' / name).chmod(0o755)
        for name in ('sr-image-id.py', 'sr-tp-prune.py', 'sr-build'):
            dst = repo / 'files' / name
            dst.write_bytes((REPO / 'files' / name).read_bytes())
            dst.chmod(0o755)
        tree = base / 'starrocks'
        (tree / 'be/src').mkdir(parents=True)
        (tree / 'build.sh').touch()
        (tree / 'env.sh').touch()
        mockbin = base / 'bin'
        mockbin.mkdir()
        docker_log = base / 'docker.log'
        docker = mockbin / 'docker'
        docker.write_text(
            '#!/bin/bash\n'
            'printf "%s\\n" "$*" >> "$DOCKER_LOG"\n'
            'if [ "$1" = inspect ]; then echo true; exit 0; fi\n'
            'case "$*" in *share/sr/image-id*) echo "$TEST_IMAGE_ID"; exit 0;; esac\n'
            'case "$*" in *"-dumpversion"*) echo "14.2.0"; exit 0;; esac\n'
            'case "$*" in *"gcc-toolset-14"*) echo "/opt/gcc-toolset-14"; exit 0;; esac\n'
            'exit 0\n'
        )
        docker.chmod(0o755)
        image_id = subprocess.check_output(
            ['python3', str(repo / 'files/sr-image-id.py'), str(repo), '', ''],
            text=True,
        ).strip()
        env = dict(
            os.environ,
            PATH=str(mockbin) + os.pathsep + os.environ['PATH'],
            BASE=str(base / 'base'),
            SR_TPDIR=str(base / 'tp'),
            SR_CONF=str(base / 'missing.conf'),
            SR_REGISTRIES=str(base / 'missing.reg'),
            DOCKER_LOG=str(docker_log),
            TEST_IMAGE_ID=image_id,
            SLICE='sr_bugfix_test_missing',
        )
        return repo, tree, mockbin, docker_log, env

    def test_build_concurrency_environment_overrides_site_configuration(self):
        configured = dict(SR_LOCAL_JOBS='16', SR_FE_UT_JOBS='8',
                          SR_LINK_JOBS='2', SR_LINK_MEM_GB='15')
        overrides = dict(SR_LOCAL_JOBS='6', SR_FE_UT_JOBS='3',
                         SR_LINK_JOBS='1', SR_LINK_MEM_GB='7')
        for supplied in ({}, dict.fromkeys(configured, ''), overrides):
            with self.subTest(supplied=supplied), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                repo, tree, mockbin, docker_log, env = self._setup_mock_repo(base)
                docker = mockbin / 'docker'
                # `docker exec -e NAME` reads the Docker client's environment;
                # checking argv alone would miss a lost or overwritten value.
                docker.write_text(docker.read_text().replace(
                    '#!/bin/bash\n',
                    '#!/bin/bash\n'
                    'for key in SR_LOCAL_JOBS SR_FE_UT_JOBS SR_LINK_JOBS SR_LINK_MEM_GB; do\n'
                    '  printf "env:%s=%s\\n" "$key" "${!key-<unset>}" >> "$DOCKER_LOG"\n'
                    'done\n', 1))
                conf = base / 'site.conf'
                contents = ''.join(f'{key}={value}\n' for key, value in configured.items())
                conf.write_text(contents)
                for key in configured:
                    env.pop(key, None)
                env.update(supplied, SR_CONF=str(conf))
                run = subprocess.run(
                    ['bash', str(repo / 'sr'), 'fe-ut', '--test', 'Demo'],
                    cwd=tree, env=env, capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                calls = docker_log.read_text()
                for key, value in configured.items():
                    expected = supplied.get(key) or value
                    self.assertIn(f'-e {key} ', calls)
                    if key in supplied:
                        self.assertIn(f'env:{key}={expected}\n', calls)
                self.assertEqual(conf.read_text(), contents)
                self.assertEqual((base / 'base/farm/srdev.conf').read_text(), contents)

    def test_sr_help_passthrough_stops_at_double_dash_and_allows_fe_without_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, tree, _, docker_log, env = self._setup_mock_repo(base)
            # `sr build -- --help` must not trigger host-side sr-build --help preview.
            run_dd = subprocess.run(
                ['bash', str(repo / 'sr'), '-v', 'main', 'build', '--fe', '--', '--help'],
                cwd=tree, env=env, capture_output=True, text=True,
            )
            self.assertEqual(run_dd.returncode, 0, run_dd.stdout + run_dd.stderr)
            calls = docker_log.read_text()
            self.assertIn('sr-build --fe -- --help', calls)
            self.assertNotIn('unshare -m', calls)

            # Pure FE (`sr build --fe` and `sr fe-ut`) needs neither -v nor /tp mount/lock.
            docker_log.unlink()
            for cmd in (['build', '--fe'], ['fe-ut', '--test', 'Demo']):
                res = subprocess.run(
                    ['bash', str(repo / 'sr'), *cmd],
                    cwd=tree, env=env, capture_output=True, text=True,
                )
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            calls = docker_log.read_text()
            self.assertIn('SR_TPDIGEST=-', calls)
            self.assertNotIn('unshare -m', calls)
            self.assertFalse(list((base / 'tp').glob('.sr-tp-ref-*')))

    @unittest.skipUnless(shutil.which('flock'), 'requires GNU flock')
    def test_build_subcommand_dash_v_is_forwarded_to_sr_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, tree, mockbin, docker_log, env = self._setup_mock_repo(base)
            dg = 'a' * 64
            tp_entry = base / 'tp' / dg
            (tp_entry / 'installed').mkdir(parents=True)
            (tp_entry / '.ready').touch()
            (tp_entry / '.gccmajor').write_text('14\n')
            reg = base / 'reg.conf'
            reg.write_text('ee registry.example.invalid/dev-env\n')
            curl = mockbin / 'curl'
            curl.write_text(
                '#!/bin/bash\n'
                'case "$*" in\n'
                '  */tags/list*) printf \'%s\\n\' \'{"name":"dev-env","tags":["latest"]}\';;\n'
                f'  */manifests/*) printf \'HTTP/1.1 200 OK\\r\\nDocker-Content-Digest: sha256:{dg}\\r\\n\\r\\n\';;\n'
                'esac\n'
            )
            curl.chmod(0o755)
            env = dict(env, SR_REGISTRIES=str(reg))
            res = subprocess.run(
                ['bash', str(repo / 'sr'), '-v', 'main', 'build', '--be', '-v'],
                cwd=tree, env=env, capture_output=True, text=True,
            )
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn('exec sr-build "$@" ' + dg + ' --be -v', docker_log.read_text())

    @unittest.skipUnless(shutil.which('flock'), 'requires GNU flock')
    def test_up_registers_farm_and_non_build_commands_skip_tp_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, tree, _, docker_log, env = self._setup_mock_repo(base)
            (base / 'tp').mkdir(parents=True)
            up = subprocess.run(['bash', str(repo / 'sr'), 'up'], cwd=tree, env=env,
                                capture_output=True, text=True)
            self.assertEqual(up.returncode, 0, up.stdout + up.stderr)
            refs = list((base / 'tp').glob('.sr-tp-ref-*'))
            self.assertEqual(len(refs), 1)
            self.assertEqual(refs[0].read_text().strip(), str(base / 'base/farm'))

            # While an exclusive lock is held on TPDIR, sr up/build-be would block,
            # whereas recovery/non-TP commands (farm status, deploy --stop, build --fe) must succeed.
            fd = os.open(base / 'tp', os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                for args in (['farm', 'status'], ['deploy', '--stop'], ['build', '--fe']):
                    res = subprocess.run(
                        ['bash', str(repo / 'sr'), *args],
                        cwd=tree, env=env, capture_output=True, text=True, timeout=5,
                    )
                    self.assertEqual(res.returncode, 0, f'{args}: {res.stdout}{res.stderr}')
            finally:
                os.close(fd)

    def test_shared_tp_fallback_prints_warning_on_tp_list_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, tree, _, docker_log, env = self._setup_mock_repo(base)
            base.chmod(0o755)
            profile = base / 'base'
            profile.mkdir()
            docker_log.touch()
            docker_log.chmod(0o666)
            drop = None
            if os.geteuid() == 0:
                # Root can write a mode-0555 directory; exercise the real
                # permission failure as an unprivileged user instead.
                os.chown(profile, 12345, 12345)

                def drop():
                    os.setgroups([])
                    os.setgid(12345)
                    os.setuid(12345)

            shared = base / 'shared_ro_tp'
            shared.mkdir()
            shared.chmod(0o555)
            try:
                env = dict(env, SR_TPDIR=str(shared), SR_USER='test-user')
                help_run = subprocess.run(['bash', str(repo / 'sr'), '--help'],
                                          env=env, preexec_fn=drop, capture_output=True, text=True)
                self.assertEqual(help_run.returncode, 0)
                self.assertEqual(help_run.stderr, '')

                tp_run = subprocess.run(['bash', str(repo / 'sr'), 'tp'],
                                        env=env, preexec_fn=drop, capture_output=True, text=True)
                self.assertEqual(tp_run.returncode, 0, tp_run.stderr)
                self.assertIn('不可写，已退回个人目录', tp_run.stderr)
            finally:
                shared.chmod(0o755)


class HelperArgumentAndFarmTests(unittest.TestCase):
    def test_sr_build_argument_errors(self):
        for args, expected in [
            (['--build-prefix'], '--build-prefix 需要一个目录'),
            (['--dist'], '--dist 需要 auto、on 或 off'),
            (['--dist', 'bogus'], '--dist 只接受 auto、on 或 off'),
        ]:
            run = subprocess.run(['bash', str(REPO / 'files/sr-build'), *args],
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
            self.assertIn(expected, run.stderr)
            self.assertNotIn('unbound variable', run.stderr)

    def test_sr_build_port_collision_and_sched_ours_checks(self):
        source = (REPO / 'files/sr-build').read_text()
        start = source.index('iceccd_alive(){')
        end = source.index('if [ "$DIST" != off ]; then', start)
        fns = source[start:end]

        with tempfile.TemporaryDirectory() as tmp:
            sock = Path(tmp) / 'missing.sock'
            script = f'''
set -uo pipefail
ICESOCK={sock}; DPORT=19740; SPORT=18740; SCHED=127.0.0.1; NETNAME=alice_farm; LOCALCAP=8
pgrep(){{ return 1; }}
hostname(){{ echo 127.0.0.1; }}
ss(){{
  if [ "$*" = "-ltnH sport = :19740" ]; then echo "LISTEN 0 128 0.0.0.0:19740 0.0.0.0:*"; return 0; fi
  return 0
}}
{fns}
ensure_local_daemon
'''
            res = subprocess.run(['bash', '-c', script], capture_output=True, text=True)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn('已被宿主机其他进程/用户占用', res.stderr)

    def test_sr_deploy_missing_heap_argument(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            if sys.platform != 'linux':
                shim_dir = Path(tmp) / 'bin'
                shim_dir.mkdir()
                rp = shim_dir / 'realpath'
                rp.write_text('#!/bin/sh\nshift; printf "%s\\n" "$1"\n')
                rp.chmod(0o755)
                env['PATH'] = str(shim_dir) + os.pathsep + env['PATH']
            for flag in ('--fe-heap', '--be-heap'):
                run = subprocess.run(['bash', str(REPO / 'files/sr-deploy'), flag],
                                     env=env, capture_output=True, text=True)
                self.assertNotEqual(run.returncode, 0)
                self.assertIn(flag, run.stderr)
                self.assertNotIn('unbound variable', run.stderr)

    def test_sr_farm_warm_if_starving_and_sched_ensure(self):
        source = (REPO / 'files/sr-farm').read_text()
        warm_start = source.index('warm_if_starving(){')
        warm_end = source.index('\nwarm_workers(){', warm_start)
        warm_fn = source[warm_start:warm_end]
        sched_start = source.index('port_is_ours(){')
        sched_end = source.index('dcount(){', sched_start)
        sched_fn = source[sched_start:sched_end]

        with tempfile.TemporaryDirectory() as tmp:
            farm = Path(tmp) / 'farm'
            farm.mkdir()
            logf = farm / 'watchdog.log'
            script_skip = f'''
set -uo pipefail
FARM={farm}; LOGF={logf}; FARM_STATUS=running; NODES="10.0.0.1"; WARM_EVERY_MIN=5
sched_q(){{ printf '200-ICECC u_farm: 1s uptime, 1 hosts\\n host1 (10.0.0.1:10245) [x] speed=0.00 jobs=0/8 load=0\\n'; }}
warm_workers(){{ return 2; }}
{warm_fn}
warm_if_starving
'''
            res_skip = subprocess.run(['bash', '-c', script_skip], capture_output=True, text=True)
            self.assertEqual(res_skip.returncode, 2)
            self.assertFalse((farm / '.last-warm').exists())

            script_ok = script_skip.replace('warm_workers(){ return 2; }', 'warm_workers(){ return 0; }')
            res_ok = subprocess.run(['bash', '-c', script_ok], capture_output=True, text=True)
            self.assertEqual(res_ok.returncode, 0)
            self.assertTrue((farm / '.last-warm').exists())

            # sched_ensure accepts sibling scheduler matching NETNAME and rejects foreign scheduler.
            sched_base = f'''
set -uo pipefail
SPORT=18740; NETNAME=alice_farm
ss(){{
  if [ "$*" = "-ltnpH sport = :18740" ]; then echo "LISTEN 0 128 0.0.0.0:18740 0.0.0.0:*"; return 0; fi
  if [ "$*" = "-ltnH sport = :18740" ]; then echo "LISTEN 0 128 0.0.0.0:18740 0.0.0.0:*"; return 0; fi
  return 0
}}
{sched_fn}
'''
            sibling_ok = subprocess.run(
                ['bash', '-c', sched_base + '\nsched_q(){ echo "200-ICECC alice_farm: 10s uptime, 1 hosts"; }\nsched_ensure\n'],
                capture_output=True, text=True,
            )
            self.assertEqual(sibling_ok.returncode, 0, sibling_ok.stderr)

            foreign_fail = subprocess.run(
                ['bash', '-c', sched_base + '\nsched_q(){ echo "200-ICECC bob_farm: 10s uptime, 1 hosts"; }\nsched_ensure\n'],
                capture_output=True, text=True,
            )
            self.assertNotEqual(foreign_fail.returncode, 0)
            self.assertIn('已被其他用户或进程占用', foreign_fail.stderr)


@unittest.skipUnless(sys.platform == 'linux' and os.geteuid() == 0
                     and os.environ.get('SR_TEST_NETNS') == 'isolated', 'isolated root container')
class StickyBitLockTests(unittest.TestCase):
    def test_linkgate_and_tp_lock_work_across_uids_in_sticky_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.chmod(0o755)
            gate_dir = root / 'gate'
            gate_dir.mkdir()
            gate_dir.chmod(0o1777)
            slot = gate_dir / 'slot.1'
            slot.touch()
            slot.chmod(0o666)

            def drop():
                os.setgroups([])
                os.setgid(12345)
                os.setuid(12345)

            env = dict(os.environ, SR_LINK_JOBS='1', SR_LINK_GATE_DIR=str(gate_dir), SR_LINK_GATE_WAIT='1')
            ok = subprocess.run(
                ['bash', str(REPO / 'files/linkgate'), 'true'],
                env=env, preexec_fn=drop, capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertEqual(ok.stderr, '')

            # When root holds slot.1, UID 12345 must actually contend on it instead of hitting EACCES/opened=0.
            fd = os.open(slot, os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                blocked = subprocess.run(
                    ['bash', str(REPO / 'files/linkgate'), 'true'],
                    env=env, preexec_fn=drop, capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(blocked.returncode, 0, blocked.stderr)
                self.assertIn('仍抢不到槽', blocked.stderr)
            finally:
                os.close(fd)

            # Same sticky-dir lockfile pattern used in sr ensure_tp (`9<"$lk"`).
            tp_dir = root / 'tp'
            tp_dir.mkdir()
            tp_dir.chmod(0o1777)
            lk = tp_dir / ('.lock.' + 'a' * 64)
            lk.touch()
            lk.chmod(0o666)
            tp_ok = subprocess.run(
                ['bash', '-c', 'set -euo pipefail; ( flock 9 ) 9<"$1"', '_', str(lk)],
                preexec_fn=drop, capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(tp_ok.returncode, 0, tp_ok.stderr)


if __name__ == '__main__':
    unittest.main()
