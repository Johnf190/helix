#!/bin/bash
# UNINSTALL_FOR_USER.sh — removes the vault programs and the sudo rule (ORDER 19). Run as:
#     sudo bash ~/helix/vault/UNINSTALL_FOR_USER.sh
# It deliberately LEAVES both vault logs and their append-only flags: the record stays —
# /var/lib/helix-vault/vault.log and the copy on the odin drive (ORDER 20).
# It does NOT put you back into docker/libvirt; it prints the commands so you can choose.
set -euo pipefail

USER_NAME="__USER__"
SUDOERS="/etc/sudoers.d/helix-vault"
BOLD=$'\e[1m'; OFF=$'\e[0m'

step() { echo; echo "${BOLD}=== $* ===${OFF}"; }

if [ "$(id -u)" -ne 0 ]; then
    echo "This must be run with sudo:  sudo bash $0"
    exit 1
fi

step "Step 1 of 3: remove the sudo rule"
if [ -e "$SUDOERS" ]; then
    rm -f "$SUDOERS"
    echo "Removed $SUDOERS"
else
    echo "$SUDOERS is not there. Nothing to do."
fi
visudo -c

step "Step 2 of 3: remove the two programs"
for f in /usr/local/sbin/helix-vault-add /usr/local/bin/helix-vault-verify; do
    if [ -e "$f" ]; then
        rm -f "$f"
        echo "Removed $f"
    else
        echo "$f is not there. Nothing to do."
    fi
done

step "Step 3 of 3: what is left on purpose"
echo "Both vault logs are kept, with their append-only flags, so nothing recorded is lost:"
ls -l /var/lib/helix-vault/vault.log 2>/dev/null && lsattr /var/lib/helix-vault/vault.log || echo "(no vault log found)"
D=__VAULT_DRIVE__/helix-vault/vault.log
ls -l "$D" 2>/dev/null && lsattr "$D" || echo "(no drive copy found — the odin drive may be unplugged)"
echo
echo "Group memberships were NOT changed. If you want $USER_NAME back in a group, run the ones you want:"
for g in docker libvirt lxd disk; do
    echo "    sudo gpasswd -a $USER_NAME $g"
done
echo "(Before the vault, $USER_NAME was in docker and libvirt only.) Then log out and back in."
