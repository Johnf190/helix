#!/usr/bin/env bash
# Update, health-check and clean BOX1, then reboot. Written by Odin 2026-10-18. Run once:  sudo bash ~/helix/workspace/machine/update_clean_reboot.sh
set -u
LOG=/var/log/helix-maintenance-$(date +%F).log
exec > >(tee -a "$LOG") 2>&1
echo "== $(date) — update, health, clean, reboot"
export DEBIAN_FRONTEND=noninteractive
echo "== 1/6 updates"
apt-get update -q
apt-get -y -q full-upgrade                      # all updates incl. the new kernel and NVIDIA driver packages
echo "== 2/6 health tools"
apt-get -y -q install smartmontools lm-sensors
echo "== 3/6 remove leftovers (old kernels, unused packages) and the package cache"
apt-get -y -q autoremove --purge
apt-get -q clean
echo "== 4/6 trim system logs to 500 MB"
journalctl --vacuum-size=500M
echo "== 5/6 security updates stay automatic"
grep -q 'APT::Periodic::Unattended-Upgrade "1"' /etc/apt/apt.conf.d/20auto-upgrades 2>/dev/null || \
  printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' > /etc/apt/apt.conf.d/20auto-upgrades
cat /etc/apt/apt.conf.d/20auto-upgrades
echo "== 6/6 health check"
for d in /dev/sda /dev/sdb; do echo "-- $d"; smartctl -H -A "$d" | grep -E "overall-health|Reallocated_Sector|Pending_Sector|Power_On_Hours|Temperature_Celsius|Wear_Leveling|Percent_Lifetime" ; done
sensors 2>/dev/null | grep -E "Package|Core 0|temp1" | head -5
df -h / __BULK__ | tail -2
systemctl --failed --no-legend
echo "== done. Log: $LOG. Rebooting in 15 seconds (Ctrl+C to cancel)…"
sleep 15
reboot
