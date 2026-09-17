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
    raw = d["endpoint"].strip().rstrip("/")
    # review r7 存疑2：显式 https:// 必须遵守（原来一律剥成 HTTP）；无协议保持 http，兼容现网内网端点
    scheme = "https" if raw.lower().startswith("https://") else "http"
    ep = raw.split("://", 1)[-1]
    return d["accessKeyID"].strip(), d["accessKeySecret"].strip(), ep, scheme


class OSS:
    """每线程复用一条到 OSS 的长连接；失败重连一次。"""

    def __init__(self, ak, sk, endpoint, bucket, scheme="http"):
        self.ak, self.sk, self.host, self.bucket = ak, sk, f"{bucket}.{endpoint}", bucket
        self.scheme = scheme
        self._local = threading.local()

    def _conn(self):
        c = getattr(self._local, "c", None)
        if c is None:
            cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
            c = cls(self.host, timeout=60)
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


# PUT body 上限。ccache 的对象最大是 PCH 结果，实测约 100MB 量级；256MB 留足余量又不至于让
# 未授权/畸形请求把容器内存吃光（codex r13#4）。
MAX_BODY = 256 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # ccache 默认 keep-alive；Python 会自动应答 Expect: 100-continue
    # 读超时：没有它，声明了大 Content-Length 却不发数据的连接会把线程永久占住（codex r13#4）。
    # StreamRequestHandler.setup() 会把它设到 socket 上。
    timeout = 120
    oss = None
    prefix = ""
    logf = None
    token = None   # review r7#4：每用户随机 Bearer 令牌；None 仅供无鉴权测试

    def _authed(self):
        # review r7#4(P1)：容器 --network host，监听 127.0.0.1 挡不住本机其他用户；无鉴权时任何人都能用所有者凭据
        # PUT 污染 / DELETE 清空共享缓存前缀。所有方法（含 GET、/_stats）都校验 Authorization: Bearer <token>。
        if self.token is None:
            return True
        if self.headers.get("Authorization", "") == f"Bearer {self.token}":
            return True
        self._reply(401, b"unauthorized\n")
        self._log(401, 0, 0.0)
        return False

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
        if not self._authed():
            return
        if self.path == "/_stats":
            with STATS["lock"]:
                snap = {k: dict(v) for k, v in STATS.items() if k != "lock"}
            for v in snap.values():
                v["avg_ms"] = round(v["ms"] / v["n"], 1) if v["n"] else 0
            return self._reply(200, (json.dumps(snap, indent=1) + "\n").encode(), "application/json")
        self._forward("GET")

    def do_HEAD(self):
        if not self._authed():
            return
        self._forward("HEAD")

    def do_PUT(self):
        # 先鉴权再读 body：原来为了 keep-alive 不错位而先读完 body，等于让未授权的人按自报 Content-Length
        # 把任意大小读进内存、或声明大长度后不发数据占住线程（codex r13#4）。
        # 现在未授权一律 401 并关闭连接——连接关了就不存在"残留 body 被当成下一条请求"的问题。
        if not self._authed():
            self.close_connection = True
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            self._reply(413 if n > MAX_BODY else 400, b"bad content-length\n")
            self.close_connection = True
            return
        body = self.rfile.read(n) if n else b""
        if len(body) != n:                      # 读不满说明连接中断，别把半截对象写进 OSS
            self.close_connection = True
            return
        self._forward("PUT", body, "application/octet-stream")

    def do_DELETE(self):
        if not self._authed():
            return
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
    ap.add_argument("--token-file", default=None, help="Bearer 令牌文件（review r7#4）：所有请求必须携带；不给则不鉴权，仅供测试")
    a = ap.parse_args()
    ak, sk, ep, scheme = load_cfg(a.config)
    Handler.oss = OSS(ak, sk, ep, a.bucket, scheme)
    if a.token_file:
        with open(os.path.expanduser(a.token_file)) as f:
            Handler.token = f.read().strip() or None
        if Handler.token is None:
            print("oss-ccache-proxy: 令牌文件为空，拒绝启动（无鉴权只允许显式不传 --token-file）", file=sys.stderr)
            sys.exit(2)
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
