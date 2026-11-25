#!/usr/bin/env python3
"""Minimal standard-library TSP client; no requests or click dependency.
Use the same endpoints as tsp-cli and reuse ~/.tsp/config.json mounted from the host."""
import json, os, re, sys, urllib.parse, urllib.request

CFG = os.path.expanduser(os.environ.get("TSP_CONFIG", "~/.tsp/config.json"))   # TSP_CONFIG lets tests use isolated credentials.

def _cfg():
    try:
        with open(CFG) as f: return json.load(f)
    except Exception as e:
        # Raise instead of exiting with 1: exists reserves 1 for confirmed absence; credential errors must return 2.
        raise RuntimeError("未找到/读不了 TSP 凭据 %s（%s）。请在宿主机跑一次 tsp login，或挂载该目录。" % (CFG, e))

def _req(path, data=None, raw=False):
    c = _cfg(); url = c["server_url"].rstrip("/") + path
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    r = urllib.request.Request(url, data=body,
        headers={"Authorization": "Bearer " + c["token"],
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(r, timeout=60) as resp:
        t = resp.read().decode("utf-8", "replace")
    return t if raw else json.loads(t)

def clusters():
    """List clusters; exit with 2 on an invalid response rather than treating it as empty."""
    r = _req("/cluster/api/cluster/list/")
    if not isinstance(r, dict) or not isinstance(r.get("detail"), list):
        _die2("TSP 响应结构不符: %s" % str(r)[:200])
    return r["detail"]

def _die2(msg):
    print(msg, file=sys.stderr); sys.exit(2)

def exists(cid):
    """Exit with 0 for an existing cluster (printing status), 1 for confirmed absence,
    or 2 for query failure. sr-farm down relies on this distinction. Credential,
    network, response-shape, and entry-parsing failures must all return 2; only a
    successfully validated list without the requested ID can establish absence."""
    try:
        cs = clusters()
        for c in cs:
            if not isinstance(c, dict) or c.get("cluster_id") in (None, ""):
                raise ValueError("列表元素缺少 cluster_id，响应结构不符: %r" % (str(c)[:120],))
            if str(c.get("cluster_id")) == str(cid):
                print(c.get("status") or c.get("cluster_status") or "exists"); sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        _die2("TSP 查询失败: %s" % e)
    sys.exit(1)

def cid(name):
    for c in clusters():
        if c.get("cluster_name") == name: return str(c["cluster_id"])
    return None

def nodes(name):
    for c in clusters():
        if c.get("cluster_name") == name:
            out = []
            for grp in ("fe", "be"):
                for n in (c.get(grp) or []):
                    out.append(n if isinstance(n, str) else n.get("ip", ""))
            return [x for x in out if x]
    return []

def spec(name):
    """Return node addresses and instance metadata for the farm summary."""
    for c in clusters():
        if c.get("cluster_name") != name:
            continue
        out = []
        for grp in ("fe", "be"):
            for n in (c.get(grp) or []):
                ip = n if isinstance(n, str) else n.get("ip", "")
                if ip:
                    out.append(ip)
        return {"nodes": out,
                "instance_type": c.get("be_instance_type") or c.get("fe_instance_type") or "?",
                "disk": c.get("be_disk_size") or c.get("fe_disk_size") or "?",
                "region": c.get("region", "?"), "csp": c.get("csp", "?")}
    return {}

def row(name):
    """Read expiry and the 22:00 cleanup switch from the HTML table; the API omits them."""
    t = _req("/cluster/list/", raw=True)
    # Match the complete table cell, never a substring of another cluster's name.
    # For example, looking up alice_farm must not return other_alice_farm_backup.
    # No exact match means the cluster is absent from the table.
    i = t.find(">" + name + "<")
    if i < 0: return {}
    s = t.rfind("<tr", 0, i); e = t.find("</tr>", i)
    cells = re.findall(r"<td[^>]*>(.*?)</td>", t[s:e], re.S)
    g = lambda k: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cells[k])).strip() if k < len(cells) else ""
    return {"status": g(7), "expire": g(10), "nightly": g(11)}

def op(cluster_id, type_, add_hours=None):
    d = {"cluster_id": cluster_id, "type": type_, "op_scope": "ALL"}
    if add_hours: d["add_hours"] = str(add_hours)
    r = _req("/cluster/update/", d)
    # Receiving JSON does not prove success. Check common success/code fields where
    # recognized; sr-farm verifies the resulting state through subsequent exists calls.
    if not isinstance(r, dict) or r.get("success") is False or r.get("code") not in (None, 0, "0", 200, "200"):
        sys.exit("TSP 操作 %s 未成功: %s" % (type_, str(r)[:200]))
    return r

def create(name, workers, duration=2, itype="8c32g", disk="20"):
    # Compilation workers need little persistent disk: preprocessed sources arrive
    # and object files leave. Use 20 GB rather than allocating 100 GB for an unused
    # StarRocks installation.
    d = {"cluster_name": name, "csp": os.environ.get("TSP_CSP", "aliyun"),
         "description": "sr-farm distributed compile workers",
         "duration": str(duration),
         "fe_instance_type": itype, "be_instance_type": itype,
         "fe_num": "1", "be_num": str(workers), "cn_num": "0",
         "fe_disk_size": disk, "be_disk_size": disk,
         "fe_disk_count": "1", "be_disk_count": "1",
         "fe_p_level": "PL1", "be_p_level": "PL1",
         "linux_type": "ubuntu", "arch": "amd64",
         "package_option": "officially release", "deploy_obj": "starrocks",
         "branch_name": "branch-4.1", "build_type": "Release",
         # package_path=null requests machines without deploying StarRocks.
         # This avoids deployment and cleanup overhead, including an oversized default FE heap.
         "region": os.environ.get("TSP_REGION", "cn-zhangjiakou"), "package_path": "null",
         "release_version": "4.1.0", "sr_conf": "daily-release",
         "data_import": "false", "wide_table": "false",
         "replication_num": "3", "data_sets": ""}
    r = _req("/cluster/quick/apply/", d)
    # Return a failure status on rejected creation. Otherwise a duplicate-name error
    # could be treated as success, causing the caller to claim an existing cluster
    # and run node-setup against its services.
    if not isinstance(r, dict) or r.get("success") is False or r.get("code") not in (None, 0, "0", 200, "200"):
        sys.exit("TSP 创建集群未成功: %s" % str(r)[:200])
    return r

def _main():
    a = sys.argv[1:]
    if not a: sys.exit("用法: tspapi.py {cid|exists|nodes|spec|row|op|create} ...")
    f = a[0]
    if   f == "cid":    print(cid(a[1]) or "")
    elif f == "exists": exists(a[1])
    elif f == "nodes":  print(" ".join(nodes(a[1])))
    elif f == "spec":
        d = spec(a[1])
        print("%s|%s|%s|%s" % (" ".join(d.get("nodes", [])), d.get("instance_type","?"),
                               d.get("disk","?"), d.get("region","?")))
    elif f == "row":    r = row(a[1]); print("%s|%s|%s" % (r.get("status",""), r.get("expire",""), r.get("nightly","")))
    elif f == "op":     print(op(a[1], a[2], a[3] if len(a) > 3 else None).get("msg",""))
    elif f == "create": print(create(a[1], int(a[2]), int(a[3]) if len(a) > 3 else 2,
                                    a[4] if len(a) > 4 else "8c32g",
                                    a[5] if len(a) > 5 else "20").get("msg",""))
    else: sys.exit("未知子命令 " + f)

if __name__ == "__main__":
    try:
        _main()
    except SystemExit:
        raise
    except Exception as e:          # Report credential/network/parsing failures briefly with a nonzero exit status, without a traceback.
        sys.exit("TSP 请求失败: %s" % e)
