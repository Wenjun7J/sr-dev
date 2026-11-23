#!/usr/bin/env python3
"""Host-side thirdparty maintenance. All cooperating users lock the cache directory."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.error import HTTPError
import urllib.request

DIGEST = re.compile(r'[0-9a-f]{64}')
PREFIX = re.compile(r'[0-9a-f]{12}(?:[0-9a-f]{52})?')
TAG_REF = re.compile(r'([A-Za-z0-9._:-]+/[A-Za-z0-9._/-]+):([A-Za-z0-9_][A-Za-z0-9_.-]*)')
REGISTRATION = '.sr-tp-ref-'
LABEL = 'org.srdev.tp-lock'
ACCEPT = ', '.join(('application/vnd.docker.distribution.manifest.v2+json',
                    'application/vnd.docker.distribution.manifest.list.v2+json',
                    'application/vnd.oci.image.manifest.v1+json',
                    'application/vnd.oci.image.index.v1+json'))


@contextmanager
def cache_lock(root, exclusive=False):
    # Lock the directory inode itself: no shared lock file to truncate or replace.
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('依赖目录正在被构建、IDE 或其他维护任务使用，请空闲后再试') from None
        yield fd
    finally:
        os.close(fd)


def read_text(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as src:
        if not stat.S_ISREG(os.fstat(src.fileno()).st_mode):
            raise RuntimeError(f'不是普通文件：{path}')
        content = src.read(65537)
        if len(content) > 65536:
            raise RuntimeError(f'元数据过大：{path}')
        return content.strip()


def register(root, farm):
    """Remember custom BASE paths even after their container has been removed."""
    name = REGISTRATION + hashlib.sha256(os.fsencode(farm)).hexdigest()
    dest = root / name
    with cache_lock(root):
        if dest.exists():
            if read_text(dest) != str(farm):
                raise RuntimeError(f'依赖引用登记不一致：{dest}')
            return
        fd, tmp = tempfile.mkstemp(prefix='.sr-tp-register-', dir=root)
        try:
            with os.fdopen(fd, 'w') as out:
                out.write(str(farm) + '\n')
                os.fchmod(out.fileno(), 0o644)
            try:
                os.link(tmp, dest)
            except FileExistsError:
                if read_text(dest) != str(farm):
                    raise RuntimeError(f'依赖引用登记不一致：{dest}')
        finally:
            os.unlink(tmp)


def docker_inventory():
    ids = subprocess.check_output(['docker', 'ps', '-aq'], text=True).split()
    if not ids:
        return []
    # Inspect in batches to avoid argv limits on shared development hosts.
    containers = []
    for start in range(0, len(ids), 100):
        containers.extend(json.loads(subprocess.check_output(
            ['docker', 'inspect', *ids[start:start + 100]], text=True)))
    return containers


def reference_farms(root, farm, containers, apply):
    farms = {farm}
    for item in root.iterdir():
        if item.name.startswith(REGISTRATION):
            value = Path(read_text(item))
            if not value.is_absolute() or value == Path('/'):
                raise RuntimeError(f'非法的引用登记：{item}')
            farms.add(value)
    # Legacy caches also have references in container mounts and default homes.
    import pwd
    for user in pwd.getpwall():
        home = Path(user.pw_dir)
        if home.is_absolute() and home != Path('/'):
            path = home / 'farm'
            try:
                if stat.S_ISDIR(path.stat().st_mode):
                    farms.add(path.resolve())
            except FileNotFoundError:
                pass
            except OSError as error:
                if apply:
                    raise
                print(f'!! 预览跳过无法读取的用户 farm {path}: {error}', file=sys.stderr)
    for container in containers:
        mounts = {m['Destination']: m for m in container.get('Mounts', [])}
        tp = mounts.get('/tp')
        if not tp:
            continue
        try:
            tp_src = Path(tp['Source']).resolve()
        except OSError as error:
            if apply:
                raise
            if os.path.normpath(tp['Source']) != str(root):
                print(f'!! 预览跳过无法读取的容器缓存挂载 {tp["Source"]}: {error}', file=sys.stderr)
                continue
            tp_src = root
        if tp_src != root:
            continue
        name = container.get('Name', container.get('Id', '?')).lstrip('/')
        if apply and (container.get('Config', {}).get('Labels') or {}).get(LABEL) != '1':
            raise RuntimeError(f'容器 {name} 尚不支持依赖锁；请先在空闲时升级共享此缓存的 srdev 容器（包括已停止的容器）')
        if '/farm' not in mounts:
            raise RuntimeError(f'无法确认容器 {name} 的 /farm 引用目录')
        try:
            farms.add(Path(mounts['/farm']['Source']).resolve())
        except OSError as error:
            if apply:
                raise
            print(f'!! 预览跳过无法读取的容器 farm 挂载 {mounts["/farm"]["Source"]}: {error}', file=sys.stderr)
    return farms


def references(farms, strict=True):
    keep = set()
    for farm in sorted(farms):
        try:
            st = farm.stat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as error:
            if strict:
                raise
            print(f'!! 预览跳过无法读取的引用目录 {farm}: {error}', file=sys.stderr)
            continue
        if not stat.S_ISDIR(st.st_mode):
            continue
        # iterdir must succeed; permission failures must never mean "no references".
        try:
            entries = list(farm.iterdir())
        except OSError as error:
            if strict:
                raise
            print(f'!! 预览跳过无法读取的引用目录 {farm}: {error}', file=sys.stderr)
            continue
        for directory in entries:
            if directory.name == 'versions':
                if directory.is_symlink():
                    raise RuntimeError(f'引用目录不能是符号链接：{directory}')
                try:
                    paths = list(directory.iterdir())
                except OSError as error:
                    if strict:
                        raise
                    print(f'!! 预览跳过无法读取的版本目录 {directory}: {error}', file=sys.stderr)
                    continue
                for path in paths:
                    if path.name.startswith('.'):
                        continue
                    try:
                        fields = read_text(path).split()
                    except OSError as error:
                        if strict:
                            raise
                        print(f'!! 预览跳过无法读取的引用 {path}: {error}', file=sys.stderr)
                        continue
                    if len(fields) != 2 or not PREFIX.fullmatch(fields[1]):
                        if strict:
                            raise RuntimeError(f'版本记录无法解析：{path}')
                        print(f'!! 预览跳过无法解析的版本记录：{path}', file=sys.stderr)
                        continue
                    keep.add(fields[1])
            elif directory.name.startswith('sr-build.') and directory.name.endswith('.run'):
                try:
                    fields = read_text(directory).split('\t')
                except OSError as error:
                    if strict:
                        raise
                    print(f'!! 预览跳过无法读取的构建记录 {directory}: {error}', file=sys.stderr)
                    continue
                dg = fields[3].rsplit('/', 1)[-1] if len(fields) >= 4 else ''
                tgt = fields[6] if len(fields) >= 7 else ''
                if tgt in ('fe', 'fe-ut') and dg in ('-', '?'):
                    continue
                if not PREFIX.fullmatch(dg):
                    if strict:
                        raise RuntimeError(f'构建记录无法解析：{directory}')
                    print(f'!! 预览跳过无法解析的构建记录：{directory}', file=sys.stderr)
                    continue
                keep.add(dg)
    return keep


def metadata(directory, repos):
    """Only complete, internally consistent, known-origin cache entries qualify."""
    if directory.is_symlink() or not directory.is_dir():
        return None
    try:
        read_text(directory / '.ready')
        image = read_text(directory / '.image')
        match = TAG_REF.fullmatch(image)
        if not match or match[1] not in repos:
            return None
        if read_text(directory / '.digest') != 'sha256:' + directory.name:
            return None
        if read_text(directory / '.ref') != match[1] + '@sha256:' + directory.name:
            return None
        if not read_text(directory / '.gccmajor').isdigit():
            return None
        installed = directory / 'installed'
        if installed.is_symlink() or not installed.is_dir():
            return None
        return image
    except (FileNotFoundError, OSError, UnicodeError, RuntimeError):
        return None


def current_digest(image):
    match = TAG_REF.fullmatch(image)
    repo, tag = match.groups()
    host, path = repo.split('/', 1)
    request = urllib.request.Request(f'http://{host}/v2/{path}/manifests/{tag}',
                                     headers={'Accept': ACCEPT}, method='HEAD')
    # Follow the same anonymous HTTP registry contract as sr, without redirects.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=10) as response:
            value = response.headers.get('Docker-Content-Digest', '')
            if response.status != 200 or not value.startswith('sha256:') or not DIGEST.fullmatch(value[7:]):
                raise RuntimeError(f'registry 未返回有效 digest：{image}')
            return value[7:]
    except HTTPError as error:
        if error.code == 404:
            tags_req = urllib.request.Request(f'http://{host}/v2/{path}/tags/list')
            try:
                with opener.open(tags_req, timeout=10) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode('utf-8', 'replace'))
                        if (isinstance(data, dict) and data.get('name') == path
                                and isinstance(data.get('tags'), list)
                                and len(data['tags']) > 0
                                and tag not in data['tags']):
                            return None
            except Exception:
                pass
        raise


def prune(root, farm, repos, apply=False, inventory=docker_inventory, lookup=current_digest):
    with cache_lock(root, exclusive=apply) as root_fd:
        farms = reference_farms(root, farm, inventory(), apply)
        pinned = references(farms, strict=apply)
        entries = {p.name: metadata(p, repos) for p in sorted(root.iterdir())
                   if DIGEST.fullmatch(p.name)}
        # Resolve every recorded tag before deleting anything, including tags whose
        # own cache is pinned: they may currently point to a different local entry.
        current = set()
        for image in sorted(set(entries.values()) - {None}):
            digest = lookup(image)
            # registry 删除历史 tag 后，旧 digest 已不再是当前来源，
            # 应按普通旧缓存参与候选判断；网络/5xx 等其他错误仍然中止。
            if digest is not None:
                current.add(digest)
        candidates = []
        for dg, image in entries.items():
            if image is None:
                reason = '元数据不完整、路径异常或来源不在当前配置中'
            elif dg in current:
                reason = '来源 tag 当前 digest'
            elif any(dg.startswith(prefix) for prefix in pinned):
                reason = '构建或 IDE 记录仍引用'
            else:
                candidates.append(dg)
                print(f'{"待删除" if apply else "可清理"} {dg}  {root / dg}')
                continue
            print(f'保留   {dg[:12]}  {reason}')
        if not apply:
            print(f'预览：{len(candidates)} 个旧 digest 可清理；执行 sr tp prune --yes 才会删除。')
            return candidates
        if not shutil.rmtree.avoids_symlink_attacks:
            raise RuntimeError('当前 Python 不支持防符号链接替换的目录删除，拒绝清理')
        # Both discovery and removal stay under the same exclusive cache lock.
        for dg in candidates:
            directory = root / dg
            current_root, locked_root = root.stat(), os.fstat(root_fd)
            if (current_root.st_dev, current_root.st_ino) != (locked_root.st_dev, locked_root.st_ino):
                raise RuntimeError('依赖根目录已被替换，拒绝删除')
            if metadata(directory, repos) != entries[dg]:
                raise RuntimeError(f'删除前依赖元数据发生变化：{directory}')
            shutil.rmtree(directory)
            print(f'已删除 {directory}', flush=True)
        print(f'清理完成：删除 {len(candidates)} 个本地依赖目录；Docker 镜像和构建产物不受影响。')
        return candidates


def main():
    parser = argparse.ArgumentParser(description='清理旧 thirdparty digest；默认只预览。')
    parser.add_argument('command', choices=['register', 'prune'])
    parser.add_argument('--root', required=True)
    parser.add_argument('--farm', required=True)
    parser.add_argument('--repo', action='append', default=[])
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--yes', '-y', action='store_true', help='实际删除；需要 sudo 权限及构建/IDE 空闲')
    group.add_argument('--dry-run', action='store_true', help='只预览（默认）')
    args = parser.parse_args()
    root, farm = Path(args.root).resolve(), Path(args.farm).resolve()
    if root in (Path('/'), Path.home()) or not root.is_dir() or farm == Path('/'):
        parser.error('需要独立的依赖目录和明确的 farm 目录')
    try:
        if args.command == 'register':
            register(root, farm)
        else:
            if args.yes and os.geteuid() != 0:
                raise PermissionError('删除 root 属主的依赖需要权限')
            prune(root, farm, set(args.repo), args.yes)
    except PermissionError as error:
        if os.geteuid() == 0 or args.command == 'register':
            print(f'!! 依赖维护中止：{error}', file=sys.stderr)
            return 1
        if not args.yes:
            print(f'!! 预览无法读取其他用户的引用：{error}', file=sys.stderr)
            return 1
        print('需要读取其他用户的引用或删除 root 属主文件，通过 sudo 执行维护程序。', file=sys.stderr)
        sudo = ['sudo'] + ([] if sys.stdin.isatty() else ['-n'])
        try:
            return subprocess.call([*sudo, '--', sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]])
        except OSError as error:
            print(f'!! 无法执行 sudo：{error}', file=sys.stderr)
            return 1
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f'!! 清理中止：{error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
