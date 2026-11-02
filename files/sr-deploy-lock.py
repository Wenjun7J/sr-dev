#!/usr/bin/env python3
"""Hold a host-network-scoped deployment lock across exec, without shared files."""
import os
import socket
import sys

lock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    lock.bind('\0srdev.deploy.ports.v1')
except OSError as error:
    print(f'!! 本机已有部署正在进行，端口分配暂忙，请稍后重试：{error}', file=sys.stderr)
    sys.exit(75)
if lock.fileno() != 200:
    os.dup2(lock.fileno(), 200, inheritable=True)
    lock.close()
else:
    os.set_inheritable(200, True)
os.execvpe('bash', ['bash', *sys.argv[1:]], dict(os.environ, SR_DEPLOY_PORT_LOCK='held'))
