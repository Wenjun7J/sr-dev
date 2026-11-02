#!/usr/bin/env python3
"""Identify the Docker build inputs; never include private site credentials."""
import hashlib
from pathlib import Path
import shlex
import sys

repo = Path(sys.argv[1])
digest = hashlib.sha256()
names = {'Dockerfile', 'sr', 'files/sr-image-id.py', 'files/clangd-host',
         'files/lldb-host', 'files/sr-vscode.py'}
for line in (repo / 'Dockerfile').read_text().splitlines():
    if line.startswith('COPY '):
        names.update(shlex.split(line)[1:-1])
for name in sorted(names):
    path = repo / name
    digest.update(name.encode() + b'\0')
    digest.update(path.read_bytes() + b'\0')
for option in sys.argv[2:]:
    digest.update(option.encode() + b'\0')
print(digest.hexdigest())
