#!/usr/bin/env python3
"""ccache http-storage -> 阿里云 OSS 的本地签名代理。

ccache 4.x 的远端后端只有 http / redis / file 三种，没有 S3/OSS。这个代理监听本机端口，
把 ccache 发来的 GET/HEAD/PUT/DELETE 原样转成带 OSS V1 签名的请求，ccache 无感。

用法:
  oss-ccache-proxy.py --bucket <桶名> [--prefix ccache/] [--listen 127.0.0.1:8384]
                      [--config ~/.ossutilconfig] [--log FILE]
ccache 侧:
  CCACHE_REMOTE_STORAGE=http://127.0.0.1:8384/            # 读写
  CCACHE_REMOTE_STORAGE=http://127.0.0.1:8384/|read-only   # 只读（普通用户）

凭据只从 ossutil 的配置文件读取（accessKeyID / accessKeySecret / endpoint），不接受命令行传入，
避免出现在 ps 和 shell 历史里。GET /_stats 返回运行计数（JSON）。
"""
import argparse, base64, configparser, hashlib, hmac, http.client, json, os, sys, threading, time
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote


def load_cfg(path):
    cp = configparser.ConfigParser()
    cp.read(os.path.expanduser(path))
    d = cp["default"]
    ep = d["endpoint"].strip().rstrip("/").split("://", 1)[-1]
    return d["accessKeyID"].strip(), d["accessKeySecret"].strip(), ep


class OSS:
    """每线程复用一条到 OSS 的长连接；失败重连一次。"""

    def __init__(self, ak, sk, endpoint, bucket):
        self.ak, self.sk, self.host, self.bucket = ak, sk, f"{bucket}.{endpoint}", bucket
        self._local = threading.local()

    def _conn(self):
        c = getattr(self._local, "c", None)
        if c is None:
            c = http.client.HTTPConnection(self.host, timeout=60)
            self._local.c = c
        return c

    def _drop(self):
        c = getattr(self._local, "c", None)
        if c is not None:
            c.close()
        self._local.c = None

    def request(self, method, key, body=None, ctype=""):
        date = formatdate(usegmt=True)
        # OSS V1 签名: VERB \n Content-MD5 \n Content-Type \n Date \n [x-oss-*] CanonicalizedResource
        sts = f"{method}\n\n{ctype}\n{date}\n/{self.bucket}/{key}"
        sig = base64.b64encode(hmac.new(self.sk.encode(), sts.encode(), hashlib.sha1).digest()).decode()
        hdrs = {"Date": date, "Authorization": f"OSS {self.ak}:{sig}"}
        if ctype:
            hdrs["Content-Type"] = ctype
        if body is not None:
            hdrs["Content-Length"] = str(len(body))
        for attempt in (0, 1):
            c = self._conn()
            try:
                c.request(method, "/" + quote(key, safe="/"), body=body, headers=hdrs)
                r = c.getresponse()
                data = r.read()  # 非 GET 也要读完，连接才能复用
                return r.status, data
            except (http.client.HTTPException, OSError):
                self._drop()
                if attempt:
                    raise


STATS = {"lock": threading.Lock()}


def bump(method, ok, nbytes, ms):
    with STATS["lock"]:
        s = STATS.setdefault(method, {"n": 0, "ok": 0, "bytes": 0, "ms": 0.0})
        s["n"] += 1
        s["ok"] += int(ok)
        s["bytes"] += nbytes
        s["ms"] += ms


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # ccache 默认 keep-alive；Python 会自动应答 Expect: 100-continue
    oss = None
    prefix = ""
    logf = None

    def _key(self):
        return self.prefix + unquote(self.path.split("?", 1)[0].lstrip("/"))

    def _reply(self, status, body=b"", ctype=None):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        if ctype:
            self.send_header("Content-Type", ctype)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _log(self, status, nbytes, ms):
        if self.logf:
            self.logf.write(f"{time.strftime('%H:%M:%S')} {self.command:6} {status} {nbytes:>9} {ms:7.1f}ms {self.path}\n")
            self.logf.flush()

    def _forward(self, method, body=None, ctype=""):
        t0 = time.time()
        try:
            st, data = self.oss.request(method, self._key(), body, ctype)
        except Exception as e:  # OSS 不可达：对 ccache 来说等同 miss，它会退回本地编译
            self._reply(502, str(e).encode())
            bump(method, False, 0, (time.time() - t0) * 1000)
            self._log(502, 0, (time.time() - t0) * 1000)
            return
        ms = (time.time() - t0) * 1000
        if method == "GET":
            if st == 200:
                self._reply(200, data, "application/octet-stream")
            else:
                self._reply(404 if st == 404 else 502)
            n = len(data) if st == 200 else 0
        elif method == "HEAD":
            self._reply(200 if st == 200 else (404 if st == 404 else 502))
            n = 0
        elif method == "PUT":
            self._reply(200 if 200 <= st < 300 else 502)
            n = len(body)
        else:  # DELETE
            self._reply(204 if st in (200, 204, 404) else 502)
            n = 0
        bump(method, 200 <= st < 300 or (method == "DELETE" and st == 404), n, ms)
        self._log(st, n, ms)

    def do_GET(self):
        if self.path == "/_stats":
            with STATS["lock"]:
                snap = {k: dict(v) for k, v in STATS.items() if k != "lock"}
            for v in snap.values():
                v["avg_ms"] = round(v["ms"] / v["n"], 1) if v["n"] else 0
            return self._reply(200, (json.dumps(snap, indent=1) + "\n").encode(), "application/json")
        self._forward("GET")

    def do_HEAD(self):
        self._forward("HEAD")

    def do_PUT(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n) if n else b""
        self._forward("PUT", body, "application/octet-stream")

    def do_DELETE(self):
        self._forward("DELETE")

    def log_message(self, *a):  # 关掉默认的 stderr 访问日志，用 _log
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--prefix", default="ccache/")
    ap.add_argument("--listen", default="127.0.0.1:8384")
    ap.add_argument("--config", default="~/.ossutilconfig")
    ap.add_argument("--log", default=None, help="逐请求日志文件（默认不记）")
    a = ap.parse_args()
    ak, sk, ep = load_cfg(a.config)
    Handler.oss = OSS(ak, sk, ep, a.bucket)
    Handler.prefix = a.prefix
    Handler.logf = open(a.log, "a") if a.log else None
    host, port = a.listen.rsplit(":", 1)
    # 默认 backlog 只有 5：-j56 的 ccache 同时连上来会丢 SYN，客户端 100ms connect-timeout 直接算 miss（实测 35/1640）
    ThreadingHTTPServer.request_queue_size = 256
    srv = ThreadingHTTPServer((host, int(port)), Handler)
    srv.daemon_threads = True
    print(f"oss-ccache-proxy: {a.listen} -> oss://{a.bucket}/{a.prefix} via {ep}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
