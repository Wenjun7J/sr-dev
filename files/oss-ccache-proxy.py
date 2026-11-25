#!/usr/bin/env python3
"""Local signing proxy from ccache HTTP storage to Alibaba Cloud OSS.

ccache 4.x supports HTTP, Redis, and file storage, but no native S3/OSS backend.
Translate GET/HEAD/PUT/DELETE requests into OSS V1-signed requests.

Usage:
  oss-ccache-proxy.py --bucket <bucket> [--prefix ccache/] [--listen 127.0.0.1:8384]
                      [--config ~/.ossutilconfig] [--log FILE]
ccache configuration:
  CCACHE_REMOTE_STORAGE=http://127.0.0.1:8384/            # Read/write.
  CCACHE_REMOTE_STORAGE=http://127.0.0.1:8384/|read-only   # Read-only sharing.

Read accessKeyID, accessKeySecret, and endpoint only from ossutil configuration,
never command-line arguments that could appear in process listings or shell history.
GET /_stats returns JSON counters."""
import argparse, base64, configparser, hashlib, hmac, http.client, json, os, sys, threading, time
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote


def load_cfg(path):
    cp = configparser.ConfigParser()
    cp.read(os.path.expanduser(path))
    d = cp["default"]
    raw = d["endpoint"].strip().rstrip("/")
    # Honor explicit https:// endpoints; preserve HTTP for endpoints without a scheme.
    scheme = "https" if raw.lower().startswith("https://") else "http"
    ep = raw.split("://", 1)[-1]
    return d["accessKeyID"].strip(), d["accessKeySecret"].strip(), ep, scheme


class OSS:
    """Reuse one persistent OSS connection per thread; reconnect once on failure."""

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
        # OSS V1 signature: VERB \n Content-MD5 \n Content-Type \n Date \n [x-oss-*] CanonicalizedResource.
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
                data = r.read()  # Consume non-GET responses too so the connection can be reused.
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


# Limit PUT bodies to 256 MB. Typical large PCH cache objects are around 100 MB;
# allow headroom without permitting malformed requests to exhaust container memory.
MAX_BODY = 256 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # ccache uses keep-alive; Python handles Expect: 100-continue automatically.
    # Bound reads so a client cannot reserve a thread forever by declaring a large
    # Content-Length and withholding the body. StreamRequestHandler applies this to the socket.
    timeout = 30       # Per-read timeout bounds thread occupancy by slow senders.
                       # Even a 100 MB loopback PUT should fit comfortably within 30 seconds.
    oss = None
    prefix = ""
    logf = None
    token = None   # Random per-user Bearer token; None is reserved for unauthenticated tests.

    def _authed(self):
        # Host networking means loopback is accessible to other local users. Authenticate
        # every method, including GET and /_stats, so another user cannot alter or delete
        # shared cache objects using the owner's credentials.
        if self.token is None:
            return True
        # Use a constant-time comparison to avoid exposing token bytes through timing differences.
        if hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {self.token}"):
            return True
        self._reply(401, b"unauthorized\n")
        self._log(401, 0, 0.0)
        return False

    # ccache requests keys shaped like /<two characters>/<hash>, but any local user
    # can connect. Reject decoded paths containing '..' or starting with '/' to keep
    # requests within the configured object prefix. None denotes rejection.
    def _key(self):
        raw = unquote(self.path.split("?", 1)[0]).lstrip("/")
        if not raw or raw.startswith("/") or "\\" in raw:
            return None
        if any(seg in ("", ".", "..") for seg in raw.split("/")):
            return None
        return self.prefix + raw

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
        key = self._key()
        if key is None:
            self._reply(400, b"bad key\n")
            self._log(400, 0, 0.0)
            return
        try:
            st, data = self.oss.request(method, key, body, ctype)
        except Exception as e:  # An unavailable OSS backend becomes a cache miss and falls back to local compilation.
            # Keep endpoint and hostname details in the owner's log; return only a generic 502 to clients.
            self._reply(502, b"upstream error\n")
            bump(method, False, 0, (time.time() - t0) * 1000)
            self._log(502, 0, (time.time() - t0) * 1000)
            if self.logf:
                self.logf.write(f"{time.strftime('%H:%M:%S')} upstream {type(e).__name__}: {e}\n"); self.logf.flush()
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
        # Authenticate before reading a request body. Otherwise an unauthorized client
        # can consume memory or hold a thread through its declared Content-Length.
        # Close rejected connections so unread body bytes cannot become another request.
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
        if len(body) != n:                      # An incomplete read means the connection ended; never upload a partial object.
            self.close_connection = True
            return
        self._forward("PUT", body, "application/octet-stream")

    def do_DELETE(self):
        if not self._authed():
            return
        self._forward("DELETE")

    def log_message(self, *a):  # Use _log instead of the default stderr access log.
        pass


class BoundedHTTPServer(ThreadingHTTPServer):
    """Bound connection concurrency before creating request threads.
    ThreadingHTTPServer creates threads before authentication, allowing slow or
    silent clients to exhaust resources. Close excess connections immediately."""

    max_conns = 64

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._slots = threading.BoundedSemaphore(self.max_conns)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.close_request(request)      # No acquired slot means no thread was started and no release is needed.
            return
        super().process_request(request, client_address)

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            try:
                self._slots.release()
            except ValueError:
                pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--prefix", default="ccache/")
    ap.add_argument("--listen", default="127.0.0.1:8384")
    ap.add_argument("--config", default="~/.ossutilconfig")
    ap.add_argument("--log", default=None, help="逐请求日志文件（默认不记）")
    ap.add_argument("--token-file", default=None, help="Bearer 令牌文件（review r7#4）：所有请求必须携带；不给则不鉴权，仅供测试")
    ap.add_argument("--max-conns", type=int, default=64, help="鉴权之前就生效的并发连接上限（codex r15#10）")
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
    # The default backlog of 5 can drop bursts from parallel ccache clients, turning short connection timeouts into misses.
    BoundedHTTPServer.request_queue_size = 256
    BoundedHTTPServer.max_conns = max(1, a.max_conns)
    srv = BoundedHTTPServer((host, int(port)), Handler)
    srv.daemon_threads = True
    print(f"oss-ccache-proxy: {a.listen} -> oss://{a.bucket}/{a.prefix} via {ep}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
