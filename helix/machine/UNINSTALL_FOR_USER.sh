#!/bin/bash
# UNINSTALL_FOR_USER.sh — takes back what INSTALL_FOR_USER.sh gave Helix (ORDER 67).
# Run as:   sudo bash ~/helix/machine/UNINSTALL_FOR_USER.sh
# Removes the sudoers rule and the program, and puts back the gdm config from before the install
# (if the installer made a backup). The log /var/log/helix-machine.log is left as a record.
set -euo pipefail

DST="/usr/local/sbin/helix-machine"
SUDOERS="/etc/sudoers.d/helix-machine"
GDM="/etc/gdm3/custom.conf"
GDM_BAK="/etc/gdm3/custom.conf.bak-helix"
BOLD=$'\e[1m'; RED=$'\e[1;31m'; OFF=$'\e[0m'

step() { echo; echo "${BOLD}=== $* ===${OFF}"; }

if [ "$(id -u)" -ne 0 ]; then
    echo "${RED}This must be run with sudo:  sudo bash $0${OFF}"
    exit 1
fi

step "Step 1 of 3: the sudoers rule"
if [ -e "$SUDOERS" ]; then rm -f "$SUDOERS"; echo "Removed $SUDOERS"; else echo "$SUDOERS is not there."; fi
visudo -c

step "Step 2 of 3: the program"
if [ -e "$DST" ]; then rm -f "$DST"; echo "Removed $DST"; else echo "$DST is not there."; fi

step "Step 3 of 3: automatic login"
if [ -f "$GDM_BAK" ]; then
    install -o root -g root -m 0644 "$GDM_BAK" "$GDM"
    rm -f "$GDM_BAK"
    echo "Restored $GDM from $GDM_BAK (the next boot asks for your password again)."
else
    echo "No $GDM_BAK — the installer never changed $GDM. Nothing to do."
fi

echo
echo "${BOLD}Done. /var/log/helix-machine.log was kept. Tell Odin 'the machine program is gone'.${OFF}"
