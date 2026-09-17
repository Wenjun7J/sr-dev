#!/bin/bash
# node-setup.sh <scheduler_ip> <role: sched|worker> <max_jobs> [netname] [scheduler_port] [apt_mirror]
# 在农场节点上执行。独立成文件 + scp 投递，避免多层嵌套引号导致变量不展开（踩过）。
set -u
SCHED=$1; ROLE=$2; MAXJ=${3:-auto}
# max_jobs=auto（默认）按本节点实际 CPU 核数定并发；也可显式传数字（内存受限时压低，如 16G 节点传 6）。
# 非纯数字/0/空一律回退到 nproc；nproc 拿不到再退 8。
case "$MAXJ" in ''|*[!0-9]*|0) MAXJ=$(nproc 2>/dev/null || echo 8);; esac
# netname 让同一台机器上不同用户的农场互不串台；端口同理（共享开发机上必须区分）
NET=${4:-srfarm}; SPORT=${5:-8765}
MIRROR=${6:-}   # 非空则把 archive/security.ubuntu.com 换成它

# --- 1) 清掉 TSP 部署的 StarRocks ---
# FE 默认 -Xmx26g，在 8c32g 节点上会把机器压垮（踩过：scheduler 节点被压到 sshd 拒连）
for u in starrocks-fe starrocks-be starrocks-cn; do
  sudo systemctl stop $u 2>/dev/null; sudo systemctl disable $u 2>/dev/null
done
for p in StarRocksFE starrocks_be BrokerBootstrap; do sudo pkill -f $p 2>/dev/null; done
sudo pkill -f 'start_fe.sh|start_be.sh|start_cn.sh|start_broker.sh' 2>/dev/null
sleep 2

# --- 1.5) 关掉自动升级 ---
# 农场 worker 是几小时就释放的临时机器，Ubuntu 的 unattended-upgrades 对它们只有坏处：
#   它由 systemd 定时器以 root 启动（不经 sudo，审计里查不到），装包时带着 systemd 反复 reload，
#   把 fwupd/packagekit/polkit/rsyslog/chronyd 以及 **iceccd** 一起停了再起。实测 2026-09-17：
#   某台节点开机后 11:00~11:05 被卷着重启 8 轮，每次重启 iceccd 都以"零历史"重新向 scheduler 注册、
#   评分清零，于是它永远拿不到编译任务（排队 67 个而它 0/16），只能人工唤醒。
#   它也正是下面那段"等 dpkg 锁"要等的人——apt-daily-upgrade.timer 上次触发时刻就在装 icecc 的同一秒。
# 只停定时器 + 关配置，不 kill 正在跑的那次：那可能把 dpkg 打断成半配置状态，下面的重试循环会等它自己结束。
sudo systemctl disable --now apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1
printf 'APT::Periodic::Update-Package-Lists "0";\nAPT::Periodic::Unattended-Upgrade "0";\n' \
  | sudo tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null 2>&1
AUTOUPG=$(systemctl is-enabled apt-daily-upgrade.timer 2>/dev/null || echo disabled)

# --- 2) 装 icecc（等开机自动升级放开 dpkg 锁）---
# 云上节点往往也连不上 archive.ubuntu.com，给了镜像源就先换（22.04 sources.list / 24.04 *.sources 都处理）
if [ -n "$MIRROR" ]; then
  sudo sed -i "s|http://\(archive\|security\).ubuntu.com|http://$MIRROR|g" /etc/apt/sources.list 2>/dev/null
  sudo sed -i "s|http://\(archive\|security\).ubuntu.com|http://$MIRROR|g" /etc/apt/sources.list.d/*.sources 2>/dev/null
fi
# 装完必须校验二进制在不在，失败就重试——原来输出吞进 /dev/null 也不查返回码，5 台里 3 台没装上却报"active"（踩过）
# 成功判据必须是 dpkg 状态 installed + icecc 用户存在，不能只看二进制在不在（踩过：
# 首次开机 unattended-upgrades 抢 dpkg 锁把安装打断成 iU「已解包未配置」，二进制在、用户没建，
# iceccd -u icecc 起不来）。conffile 交互提示 DEBIAN_FRONTEND 压不住，要 --force-confold（踩过：
# 半配置后再 configure 弹 "icecc.conf (Y/I/N/O/D/Z)?" 无人应答直接失败）。
APT="sudo DEBIAN_FRONTEND=noninteractive apt-get -y -qq -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"
icecc_ok(){ [ "$(dpkg-query -W -f='${Status}' icecc 2>/dev/null)" = "install ok installed" ] && id icecc >/dev/null 2>&1; }
APT_OK=0
for attempt in 1 2 3 4; do
  for i in $(seq 1 30); do sudo fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock >/dev/null 2>&1 || break; sleep 10; done
  sudo DEBIAN_FRONTEND=noninteractive dpkg --configure -a --force-confold >>/tmp/apt.log 2>&1
  icecc_ok && { APT_OK=1; break; }
  $APT update >>/tmp/apt.log 2>&1
  [ "$(dpkg-query -W -f='${Status}' icecc 2>/dev/null)" = "install ok unpacked" ] && $APT purge icecc >>/tmp/apt.log 2>&1
  $APT install icecc >>/tmp/apt.log 2>&1
  icecc_ok && { APT_OK=1; break; }
  sleep 15
done

# --- 3) 写配置 ---
sudo mkdir -p /etc/icecc
{ echo "ICECC_NETNAME=\"$NET\""
  echo "ICECC_SCHEDULER_HOST=\"$SCHED:$SPORT\""
  echo "ICECC_MAX_JOBS=\"$MAXJ\""
  echo "ICECC_ALLOW_REMOTE=\"yes\""
  echo "ICECC_NICE_LEVEL=\"5\""
} | sudo tee /etc/icecc/icecc.conf >/dev/null

# --- 4) 按角色起服务 ---
# icecc 的 unit 是 LSB init 脚本的兼容封装，`systemctl disable --now` 停不掉它（踩过），
# 必须走 init.d 才能真正停；所以先 init.d stop，再 systemctl 管开机自启。
stop_hard(){ sudo /etc/init.d/$1 stop >/dev/null 2>&1; sudo systemctl stop $1 >/dev/null 2>&1
             sudo systemctl disable $1 >/dev/null 2>&1; sudo pkill -x $1 >/dev/null 2>&1; }
if [ "$ROLE" = sched ]; then
  stop_hard iceccd                       # scheduler 专职调度，不接编译任务
  sudo systemctl enable icecc-scheduler >/dev/null 2>&1
  sudo systemctl restart icecc-scheduler
else
  stop_hard icecc-scheduler
  sudo systemctl enable iceccd >/dev/null 2>&1
  sudo systemctl restart iceccd
fi
sleep 2

SR=$(pgrep -c -f 'StarRocksFE|starrocks_be|BrokerBootstrap'); SR=${SR:-0}
# 必须回读文件，不能回显入参——回显入参的话写失败也会报成功（踩过：配置迁移
# 「成功」了但节点上还是旧值，查了很久才发现是观测和动作走了同一条链路）
RB_HOST=$(sed -n 's/^ICECC_SCHEDULER_HOST="\(.*\)"$/\1/p' /etc/icecc/icecc.conf 2>/dev/null)
RB_NET=$(sed -n 's/^ICECC_NETNAME="\(.*\)"$/\1/p' /etc/icecc/icecc.conf 2>/dev/null)
RB_JOBS=$(sed -n 's/^ICECC_MAX_JOBS="\(.*\)"$/\1/p' /etc/icecc/icecc.conf 2>/dev/null)   # 回读实际写入的并发数
OK=$([ "$RB_HOST" = "$SCHED:$SPORT" ] && [ "$RB_NET" = "$NET" ] && echo "写入OK" || echo "写入不符!")
ICE_STATE=$(systemctl is-active iceccd 2>/dev/null); icecc_ok || ICE_STATE="未装好(dpkg=$(dpkg-query -W -f='${Status}' icecc 2>/dev/null | awk '{print $3}'),见/tmp/apt.log)"
printf "%-15s role=%-6s 回读sched=%-21s 回读net=%-11s -m%-3s %s iceccd=%-8s sched服务=%-8s 自动升级=%-8s SR残留=%s 内存=%sG\n" \
  "$(hostname -I|awk '{print $1}')" "$ROLE" "${RB_HOST:-空}" "${RB_NET:-空}" "${RB_JOBS:-?}" "$OK" \
  "$ICE_STATE" "$(systemctl is-active icecc-scheduler)" "${AUTOUPG:-?}" \
  "$SR" "$(free -g|awk '/Mem:/{print $7}')"

# 退出码要反映真实结果：原来无论装没装上、配置写没写对、服务起没起来，最后一条 printf 成功就返回 0，
# 调用方只能靠事后的 worker 连接检查兜底（codex r13#6）。这里把三件事都并进退出码。
RC=0
[ "$APT_OK" = 1 ] || { echo "   !! icecc 未安装成功（见 /tmp/apt.log）" >&2; RC=1; }
[ "$OK" = "写入OK" ] || { echo "   !! /etc/icecc/icecc.conf 回读与期望不符" >&2; RC=1; }
[ -n "$RB_JOBS" ] || { echo "   !! 回读不到 ICECC_MAX_JOBS" >&2; RC=1; }
if [ "$ROLE" = sched ]; then
  [ "$(systemctl is-active icecc-scheduler)" = active ] || { echo "   !! icecc-scheduler 未处于 active" >&2; RC=1; }
else
  [ "$(systemctl is-active iceccd)" = active ] || { echo "   !! iceccd 未处于 active" >&2; RC=1; }
fi
exit $RC
