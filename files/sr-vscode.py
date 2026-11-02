#!/usr/bin/env python3
"""Install the sr deploy attach configuration without rewriting existing JSONC."""
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import fcntl
import argparse

NAME = '附加到 sr deploy 起的 starrocks_be（lldb）'
FE_NAME = '附加到 sr deploy 起的 FE（Java）'
ADAPTER = '${env:HOME}/bin/lldb-dap'
TOKEN = re.compile(r'\s+|//[^\r\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|[{}\[\]:,]|[^\s{}\[\]:,]+')


class Node:
    def __init__(self, value, start, end, children=None):
        self.value, self.start, self.end = value, start, end
        self.children = children


def parse(text):
    """JSON with comments/trailing commas; retain spans for small text edits."""
    tokens = [m for m in TOKEN.finditer(text)
              if not m[0].isspace() and not m[0].startswith(('//', '/*'))]
    pos = 0

    def take():
        nonlocal pos
        if pos >= len(tokens):
            raise ValueError('JSON 未结束')
        token = tokens[pos]
        pos += 1
        return token

    def peek():
        return tokens[pos][0] if pos < len(tokens) else ''

    def value():
        token = take()
        opening = token[0]
        if opening not in ('{', '['):
            return Node(json.loads(opening), token.start(), token.end())
        obj = opening == '{'
        close = '}' if obj else ']'
        children = {} if obj else []
        while peek() != close:
            if obj:
                key = json.loads(take()[0])
                if not isinstance(key, str) or key in children:
                    raise ValueError('JSON 对象的键重复或不是字符串')
                if take()[0] != ':':
                    raise ValueError('JSON 缺少冒号')
                children[key] = value()
            else:
                children.append(value())
            if peek() == close:
                break
            if take()[0] != ',':
                raise ValueError('JSON 缺少逗号')
        end = take().end()
        data = ({k: v.value for k, v in children.items()} if obj
                else [v.value for v in children])
        return Node(data, token.start(), end, children)

    root = value()
    if pos != len(tokens) or not isinstance(root.value, dict):
        raise ValueError('launch.json 必须是一个 JSON 对象')
    return root


def encoded(value):
    return json.dumps(value, ensure_ascii=False, indent=4)


def append(text, node, item, edits, indent=4):
    children = list(node.children.values()) if isinstance(node.children, dict) else node.children
    comma_at = None
    if children:
        last = children[-1]
        tail = text[last.end:node.end - 1]
        # A comma can precede trailing comments. Ignore commas inside comments.
        meaningful = [m[0] for m in TOKEN.finditer(tail)
                      if not m[0].isspace() and not m[0].startswith(('//', '/*'))]
        if ',' not in meaningful:
            comma_at = last.end
    end = node.end - 1
    line_start = text.rfind('\n', 0, end) + 1
    if not text[line_start:end].strip():
        end = line_start
    item = '\n'.join(' ' * indent + line for line in item.splitlines())
    prefix = ',' if comma_at == end else ''
    if comma_at is not None and comma_at != end:
        edits.append((comma_at, comma_at, ','))
    edits.append((end, end, prefix + '\n' + item + '\n'))


def merge(text, deploy, be=True, fe_port=None):
    if be:
        text = merge_config(text, {
        'name': NAME, 'type': 'lldb-dap', 'request': 'attach',
        'debugAdapterExecutable': ADAPTER,
        'program': str(Path(deploy) / 'be/lib/starrocks_be'),
        'cwd': '${workspaceFolder}', 'waitFor': False,
        'sourceMap': [[f'be/nj/build_{kind}', f'${{workspaceFolder}}/be/nj/build_{kind}']
                      for kind in ('Release', 'Debug', 'ASAN')],
        }, ('debugAdapterExecutable', 'program'))
    if fe_port is not None:
        text = merge_config(text, {
            'name': FE_NAME, 'type': 'java', 'request': 'attach',
            'hostName': '127.0.0.1', 'port': fe_port, 'projectName': 'fe-core',
            'sourcePaths': ['${workspaceFolder}/fe/fe-core/src/main/java'],
        }, ('hostName', 'port'))
    return text


def merge_config(text, config, managed_fields):
    root = parse(text)
    configs = root.children.get('configurations')
    edits = []
    if configs is None:
        append(text, root, '"configurations": ' + encoded([config]), edits)
    else:
        if not isinstance(configs.value, list) or any(not isinstance(c.value, dict) for c in configs.children):
            raise ValueError('configurations 必须是对象数组')
        matches = [c for c in configs.children if c.value.get('name') == config['name']]
        if len(matches) > 1:
            raise ValueError('存在多个同名 sr 调试配置，请先消除歧义')
        if not matches:
            append(text, configs, encoded(config), edits, indent=8)
        else:
            current = matches[0]
            if current.value.get('type') != config['type'] or current.value.get('request') != 'attach':
                raise ValueError('同名配置的调试类型不匹配，已保留原配置')
            # Only these two fields belong to sr. Keep custom breakpoints,
            # sourceMap, commands, and every unrelated configuration verbatim.
            missing = {}
            for key in managed_fields:
                field = current.children.get(key)
                if field is None:
                    missing[key] = config[key]
                elif field.value != config[key]:
                    edits.append((field.start, field.end, encoded(config[key])))
            if missing:
                fields = ',\n'.join(json.dumps(k) + ': ' + encoded(v) for k, v in missing.items())
                append(text, current, fields, edits, indent=12)
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    parse(text)
    return text


def install_link(repo, name, relative):
    target = repo / relative
    if not os.access(target, os.X_OK):
        raise ValueError(f'调试入口不存在或不可执行：{target}')
    link = Path.home() / 'bin' / name
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() and link.resolve() == target.resolve():
        return
    if link.exists() or link.is_symlink():
        raise ValueError(f'{link} 已存在且不指向 {target}，请检查入口；未覆盖')
    try:
        link.symlink_to(target)
    except FileExistsError:
        if not link.is_symlink() or link.resolve() != target.resolve():
            raise


def install_adapter(repo):
    install_link(repo, 'lldb-dap', 'files/lldb-host')


def install_entries(repo):
    for name, relative in [('sr', 'sr'), ('clangd', 'files/clangd-host'),
                           ('lldb-dap', 'files/lldb-host'), ('lldb', 'files/lldb-host')]:
        install_link(repo.resolve(), name, relative)


def atomic_write(path, text):
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def workspace_root(start):
    root = start.resolve()
    while not ((root / 'build.sh').is_file() and (root / 'be/src').is_dir()):
        if root == root.parent:
            raise ValueError('当前目录不在 StarRocks 源码树内')
        root = root.parent
    if (root / '.vscode').is_symlink():
        raise ValueError('.vscode 是软链接，未修改共享目标')
    return root


def install_clangd(start, repo):
    root = workspace_root(start)
    folder = root / '.vscode'
    folder.mkdir(exist_ok=True)
    path = folder / 'settings.json'
    with (folder / '.sr-settings.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.is_symlink():
            raise ValueError(f'{path} 是软链接，未修改共享目标')
        before = path.read_text() if path.exists() else '{\n}\n'
        node = parse(before)
        desired = str(Path.home() / 'bin/clangd')
        field = node.children.get('clangd.path')
        if field is not None and field.value != desired:
            print(f'   保留已有 clangd.path：{field.value}；sr 入口为 {desired}')
            return
        install_link(repo.resolve(), 'clangd', 'files/clangd-host')
        if field is None:
            edits = []
            append(before, node, '"clangd.path": ' + encoded(desired), edits)
            after = before
            for a, b, value in sorted(edits, reverse=True):
                after = after[:a] + value + after[b:]
            parse(after)
            atomic_write(path, after)
    print(f'== clangd 配置已就绪：{path}（需远端安装 vscode-clangd 扩展）')


def install(start, repo, deploy, be=True, fe_port=None):
    root = workspace_root(start)
    # Match sr-deploy's component/available-output selection.
    be = be and os.access(root / 'output/be/lib/starrocks_be', os.X_OK)
    fe_lib = root / 'output/fe/lib'
    if not fe_lib.is_dir() or not any(fe_lib.iterdir()):
        fe_port = None
    if not be and fe_port is None:
        return
    folder = root / '.vscode'
    folder.mkdir(exist_ok=True)
    path = folder / 'launch.json'
    with (folder / '.sr-launch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.is_symlink():
            raise ValueError(f'{path} 是软链接，未修改共享目标')
        before = path.read_text() if path.exists() else '{\n    "version": "0.2.0"\n}\n'
        after = merge(before, deploy, be, fe_port)
        if be:
            install_adapter(repo.resolve())
        if after != before or not path.exists():
            atomic_write(path, after)
    print(f'== VS Code 调试配置已就绪：{path}')
    if be:
        print(f'   {NAME}（需安装 LLVM lldb-dap 扩展）')
    if fe_port is not None:
        print(f'   {FE_NAME}：127.0.0.1:{fe_port}（需安装 Debugger for Java 扩展）')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('repo', type=Path)
    parser.add_argument('deploy')
    parser.add_argument('--no-be', action='store_true')
    parser.add_argument('--fe-port', type=int)
    parser.add_argument('--ide-only', action='store_true')
    parser.add_argument('--install-entries', action='store_true')
    args = parser.parse_args()
    try:
        if args.fe_port is not None and not 1 <= args.fe_port <= 65535:
            raise ValueError('FE 调试端口必须为 1..65535')
        if args.install_entries:
            install_entries(args.repo)
        elif args.ide_only:
            install_clangd(args.source, args.repo)
        else:
            install(args.source, args.repo, args.deploy, not args.no_be, args.fe_port)
            install_clangd(args.source, args.repo)
    except (OSError, ValueError) as error:
        print(f'!! 调试配置未生成：{error}', file=sys.stderr)
        sys.exit(1)
