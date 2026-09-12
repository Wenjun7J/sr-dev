#!/usr/bin/env python3
"""极简 TSP 客户端 —— 只用标准库，不需要 pip/requests/click。
   端点与 tsp-cli 保持一致；token 复用 ~/.tsp/config.json（由宿主机卷挂入）。"""
import json, os, re, sys, urllib.parse, urllib.request

CFG = os.path.expanduser("~/.tsp/config.json")

def _cfg():
    try:
        with open(CFG) as f: return json.load(f)
    except Exception:
        sys.exit("未找到 TSP 凭据 %s。请在宿主机跑一次 tsp login，或挂载该目录。" % CFG)

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
    return _req("/cluster/api/cluster/list/").get("detail", [])

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
    """返回每个节点的 ip / cpu / 内存 / 磁盘，建完农场时打印给用户看。"""
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
    """网页表格里才有到期时间和 22 点清扫开关（API 不返回）"""
    t = _req("/cluster/list/", raw=True)
    i = t.find(">" + name + "<")
    if i < 0: i = t.find(name)
    if i < 0: return {}
    s = t.rfind("<tr", 0, i); e = t.find("</tr>", i)
    cells = re.findall(r"<td[^>]*>(.*?)</td>", t[s:e], re.S)
    g = lambda k: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cells[k])).strip() if k < len(cells) else ""
    return {"status": g(7), "expire": g(10), "nightly": g(11)}

def op(cluster_id, type_, add_hours=None):
    d = {"cluster_id": cluster_id, "type": type_, "op_scope": "ALL"}
    if add_hours: d["add_hours"] = str(add_hours)
    return _req("/cluster/update/", d)

def create(name, workers, duration=2, itype="8c32g", disk="20"):
    # disk 20 而不是 100：实测 worker 上 icecc 只占 4 KB——预处理源码进来、.o 出去，
    # 不落任何持久数据。原先那 100G 数据盘 8~10% 的占用全是 TSP 部署的 StarRocks。
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
         # package_path=null 让平台不部署 StarRocks：农场只要机器，不要数据库。
         # 省掉部署时间，也省掉事后清理（FE 默认 -Xmx26g 会把 8c32g 压垮）。
         "region": os.environ.get("TSP_REGION", "cn-zhangjiakou"), "package_path": "null",
         "release_version": "4.1.0", "sr_conf": "daily-release",
         "data_import": "false", "wide_table": "false",
         "replication_num": "3", "data_sets": ""}
    return _req("/cluster/quick/apply/", d)

if __name__ == "__main__":
    a = sys.argv[1:]
    if not a: sys.exit("用法: tspapi.py {cid|nodes|spec|row|op|create} ...")
    f = a[0]
    if   f == "cid":    print(cid(a[1]) or "")
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
