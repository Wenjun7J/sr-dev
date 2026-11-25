#!/bin/bash
# node-setup.sh <scheduler_ip> <role: sched|worker> <max_jobs> [netname] [scheduler_port] [apt_mirror]
# Copy and run this standalone script on farm nodes to avoid nested quoting and expansion errors.
set -u
SCHED=$1; ROLE=$2; MAXJ=${3:-auto}
# max_jobs=auto derives concurrency from the node's CPUs. An explicit smaller
# number can help memory-limited nodes. Empty, zero, or nonnumeric values fall
# back to nproc, then to 8 if CPU detection fails.
case "$MAXJ" in ''|*[!0-9]*|0) MAXJ=$(nproc 2>/dev/null || echo 8);; esac
# Separate network names and ports prevent different users' farms from mixing on a shared host.
NET=${4:-srfarm}; SPORT=${5:-8765}
MIRROR=${6:-}   # A nonempty mirror replaces archive/security.ubuntu.com.

# 1. Stop StarRocks services provisioned by TSP.
# The default 26g FE heap can overwhelm an 8c32g compilation node.
for u in starrocks-fe starrocks-be starrocks-cn; do
  sudo systemctl stop $u 2>/dev/null; sudo systemctl disable $u 2>/dev/null
done
for p in StarRocksFE starrocks_be BrokerBootstrap; do sudo pkill -f $p 2>/dev/null; done
sudo pkill -f 'start_fe.sh|start_be.sh|start_cn.sh|start_broker.sh' 2>/dev/null
sleep 2

# 1.5. Disable automatic upgrades on these short-lived compilation workers.
# Unattended upgrades can repeatedly restart iceccd and reset its scheduler score,
# leaving the node without tasks. They can also hold the dpkg lock during setup.
# Disable timers and future upgrades, but do not kill an active package operation:
# that could leave dpkg half-configured. The installation loop below waits for it.
sudo systemctl disable --now apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1
printf 'APT::Periodic::Update-Package-Lists "0";\nAPT::Periodic::Unattended-Upgrade "0";\n' \
  | sudo tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null 2>&1
AUTOUPG=$(systemctl is-enabled apt-daily-upgrade.timer 2>/dev/null || echo disabled)

# 2. Install icecc after any boot-time upgrades release the dpkg lock.
# Apply the optional mirror to both sources.list and deb822 *.sources files.
if [ -n "$MIRROR" ]; then
  sudo sed -i "s|http://\(archive\|security\).ubuntu.com|http://$MIRROR|g" /etc/apt/sources.list 2>/dev/null
  sudo sed -i "s|http://\(archive\|security\).ubuntu.com|http://$MIRROR|g" /etc/apt/sources.list.d/*.sources 2>/dev/null
fi
# Retry failed installations and verify both dpkg's installed state and the icecc
# user. Binaries may exist after unpacking even though configuration never finished.
# Use --force-confold: DEBIAN_FRONTEND alone does not suppress conffile prompts
# when recovering from a partially configured package.
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

# 3. Write configuration.
sudo mkdir -p /etc/icecc
{ echo "ICECC_NETNAME=\"$NET\""
  echo "ICECC_SCHEDULER_HOST=\"$SCHED:$SPORT\""
  echo "ICECC_MAX_JOBS=\"$MAXJ\""
  echo "ICECC_ALLOW_REMOTE=\"yes\""
  echo "ICECC_NICE_LEVEL=\"5\""
} | sudo tee /etc/icecc/icecc.conf >/dev/null

# 4. Start services according to the node role.
# The icecc systemd unit wraps an LSB script; stop it through init.d first,
# then use systemctl to control startup at boot.
stop_hard(){ sudo /etc/init.d/$1 stop >/dev/null 2>&1; sudo systemctl stop $1 >/dev/null 2>&1
             sudo systemctl disable $1 >/dev/null 2>&1; sudo pkill -x $1 >/dev/null 2>&1; }
if [ "$ROLE" = sched ]; then
  stop_hard iceccd                       # The scheduler only schedules jobs; it does not compile them.
  sudo systemctl enable icecc-scheduler >/dev/null 2>&1
  sudo systemctl restart icecc-scheduler
else
  stop_hard icecc-scheduler
  sudo systemctl enable iceccd >/dev/null 2>&1
  sudo systemctl restart iceccd
fi
sleep 2

SR=$(pgrep -c -f 'StarRocksFE|starrocks_be|BrokerBootstrap'); SR=${SR:-0}
# Read configuration back from disk instead of echoing input arguments,
# otherwise a failed write could still be reported as successful.
RB_HOST=$(sed -n 's/^ICECC_SCHEDULER_HOST="\(.*\)"$/\1/p' /etc/icecc/icecc.conf 2>/dev/null)
RB_NET=$(sed -n 's/^ICECC_NETNAME="\(.*\)"$/\1/p' /etc/icecc/icecc.conf 2>/dev/null)
RB_JOBS=$(sed -n 's/^ICECC_MAX_JOBS="\(.*\)"$/\1/p' /etc/icecc/icecc.conf 2>/dev/null)   # Read back the concurrency value actually written.
OK=$([ "$RB_HOST" = "$SCHED:$SPORT" ] && [ "$RB_NET" = "$NET" ] && echo "写入OK" || echo "写入不符!")
ICE_STATE=$(systemctl is-active iceccd 2>/dev/null); icecc_ok || ICE_STATE="未装好(dpkg=$(dpkg-query -W -f='${Status}' icecc 2>/dev/null | awk '{print $3}'),见/tmp/apt.log)"
printf "%-15s role=%-6s 回读sched=%-21s 回读net=%-11s -m%-3s %s iceccd=%-8s sched服务=%-8s 自动升级=%-8s SR残留=%s 内存=%sG\n" \
  "$(hostname -I|awk '{print $1}')" "$ROLE" "${RB_HOST:-空}" "${RB_NET:-空}" "${RB_JOBS:-?}" "$OK" \
  "$ICE_STATE" "$(systemctl is-active icecc-scheduler)" "${AUTOUPG:-?}" \
  "$SR" "$(free -g|awk '/Mem:/{print $7}')"

# Report installation, configuration, and service health through the exit code.
# A successful final printf must not mask failures or leave worker connectivity
# as the caller's only indication that provisioning failed.
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
