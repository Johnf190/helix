#!/bin/bash
# INSTALL_FOR_USER.sh — installs or upgrades the Helix vault (ORDER 19; drive copy ORDER 20).
# Run in a terminal, with the odin drive plugged in, as:
#     sudo bash ~/helix/vault/INSTALL_FOR_USER.sh
# It prints every step before doing it and stops at the first error.
# Running it again is safe: steps that are already done are left as they are.
set -euo pipefail

USER_NAME="__USER__"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADD_DST="/usr/local/sbin/helix-vault-add"
VERIFY_DST="/usr/local/bin/helix-vault-verify"
VAULT_DIR="/var/lib/helix-vault"
LOG="$VAULT_DIR/vault.log"
SUDOERS="/etc/sudoers.d/helix-vault"
DRIVE_MNT="__VAULT_DRIVE__"
DRIVE_DIR="$DRIVE_MNT/helix-vault"
DRIVE_LOG="$DRIVE_DIR/vault.log"
RULE="$USER_NAME ALL=(root) NOPASSWD: $ADD_DST"
RED=$'\e[1;31m'; GREEN=$'\e[1;32m'; BOLD=$'\e[1m'; OFF=$'\e[0m'

step() { echo; echo "${BOLD}=== $* ===${OFF}"; }
red()  { echo "${RED}$*${OFF}"; }

if [ "$(id -u)" -ne 0 ]; then
    red "This must be run with sudo:  sudo bash $HERE/INSTALL_FOR_USER.sh"
    exit 1
fi

# Copy both programs into a root-only folder first. Everything below hashes and installs these
# copies, so nobody can swap the files in $HERE while this script waits for you.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp "$HERE/helix-vault-add" "$STAGE/helix-vault-add"
cp "$HERE/helix-vault-verify" "$STAGE/helix-vault-verify"

step "Step 1 of 11: check the odin drive is plugged in (nothing is changed before this passes)"
FSTYPE="$(findmnt -n -o FSTYPE --mountpoint "$DRIVE_MNT" || true)"
echo "$DRIVE_MNT: ${FSTYPE:-not mounted}"
if [ "$FSTYPE" != "ext4" ]; then
    red "The odin drive is not mounted at $DRIVE_MNT (or is not ext4)."
    red "Plug in / mount the odin drive (open it once in the file manager), then run this again."
    red "Nothing has been changed."
    exit 1
fi

step "Step 2 of 11: fingerprints of the two programs"
sha256sum "$STAGE/helix-vault-add" "$STAGE/helix-vault-verify"
echo "Compare these with the box at the top of ~/helix/build_orders/reports/ORDER_20_report.md."
echo "If they do NOT match exactly, press Ctrl+C now and tell Odin."
read -r -p "If they match, press Enter to continue... "

step "Step 3 of 11: install the programs (owned by root, nobody else can change them)"
install -o root -g root -m 0755 "$STAGE/helix-vault-add" "$ADD_DST"
install -o root -g root -m 0755 "$STAGE/helix-vault-verify" "$VERIFY_DST"
ls -l "$ADD_DST" "$VERIFY_DST"

step "Step 4 of 11: create the vault log and make it append-only"
install -d -o root -g root -m 0755 "$VAULT_DIR"
if [ -e "$LOG" ]; then
    echo "$LOG already exists — leaving it exactly as it is."
else
    install -o root -g root -m 0644 /dev/null "$LOG"
    echo "Created empty $LOG"
fi
chattr +a "$LOG"
ls -l "$LOG"
lsattr "$LOG"
if ! lsattr "$LOG" | cut -d' ' -f1 | grep -q a; then
    red "The append-only flag 'a' is NOT set. Stop here and tell Odin."
    exit 1
fi
echo "${GREEN}The 'a' in the line above means: even root's programs can only add to the end.${OFF}"

step "Step 5 of 11: allow $USER_NAME to run ONLY helix-vault-add as root, with no password"
if [ -f "$SUDOERS" ] && [ ! -L "$SUDOERS" ] && [ "$(cat "$SUDOERS")" = "$RULE" ]; then
    echo "The rule is already in place. Leaving it as it is."
else
    TMP="$(mktemp)"
    echo "$RULE" > "$TMP"
    chmod 0440 "$TMP"
    visudo -cf "$TMP"
    install -o root -g root -m 0440 "$TMP" "$SUDOERS"
    rm -f "$TMP"
fi
echo "Now in $SUDOERS:"
cat "$SUDOERS"
visudo -c

step "Step 6 of 11: close the back doors to root"
id "$USER_NAME"
REMOVED=0
MEMBER_OF=()
for g in docker libvirt lxd disk; do
    if id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx "$g"; then
        MEMBER_OF+=("$g")
    fi
done
if [ "${#MEMBER_OF[@]}" -eq 0 ]; then
    echo "$USER_NAME is in none of docker, libvirt, lxd, disk. Nothing to do."
else
    echo "$USER_NAME is in: ${MEMBER_OF[*]}"
    echo "Each of these groups lets a user become root without a password"
    echo "(docker/lxd: start a container that mounts / ; libvirt: a VM that mounts the host disk;"
    echo " disk: write the raw disk). While they stay, Odin could get round the vault."
    echo
    echo "What changes for you if they are removed:"
    echo "  - Your docker images and the 7 lab VMs (DC01, WS01-WS06, all shut off) are NOT touched."
    echo "    They still exist and still work."
    echo "  - You just type 'sudo docker ...' and 'sudo virsh ...' instead of 'docker ...' / 'virsh ...'."
    echo "  - virt-manager will ask for your password when it connects."
    echo "  - Programs already running (including Helix) keep these groups until a REBOOT."
    read -r -p "Remove $USER_NAME from ${MEMBER_OF[*]} now? [y/N] " ans
    if [ "${ans:-}" = "y" ] || [ "${ans:-}" = "Y" ]; then
        for g in "${MEMBER_OF[@]}"; do
            echo "gpasswd -d $USER_NAME $g"
            gpasswd -d "$USER_NAME" "$g"
        done
        REMOVED=1
    else
        red "SKIPPED. The vault is NOT tamper-proof until $USER_NAME leaves: ${MEMBER_OF[*]}"
        red "Run this script again when you are ready."
    fi
fi

step "Step 7 of 11: the sudo picture — please read it"
echo "--- sudo -l -U $USER_NAME ---"
sudo -l -U "$USER_NAME" || true
echo
echo "--- files with a NOPASSWD rule (expected: only $SUDOERS) ---"
FOUND="$(grep -l NOPASSWD /etc/sudoers.d/* /etc/sudoers 2>/dev/null || true)"
echo "${FOUND:-none}"
for f in $FOUND; do
    if [ "$f" != "$SUDOERS" ]; then
        red "UNEXPECTED NOPASSWD rule in $f (not changed — tell Odin):"
        grep -n NOPASSWD "$f" | sed "s/^/    /" | while IFS= read -r l; do red "$l"; done
    elif [ "$(grep -c NOPASSWD "$f")" -ne 1 ] || [ "$(cat "$f")" != "$RULE" ]; then
        red "$f does not contain exactly the one expected line. Tell Odin."
    fi
done
echo
echo "--- how long a typed sudo password is remembered, and whether per-terminal ---"
sudo -V | grep -i 'timestamp\|tty' || true

step "Step 8 of 11: the vault folder on the odin drive (owned by root)"
if [ -L "$DRIVE_DIR" ] || { [ -e "$DRIVE_DIR" ] && [ ! -d "$DRIVE_DIR" ]; }; then
    red "$DRIVE_DIR exists but is not a plain folder. Not touching it. Stop here and tell Odin."
    exit 1
fi
if [ -d "$DRIVE_DIR" ] && [ "$(stat -c %u "$DRIVE_DIR")" != "0" ]; then
    red "$DRIVE_DIR exists but is not owned by root, so this script did not make it."
    red "Not touching it. Stop here and tell Odin."
    exit 1
fi
install -d -o root -g root -m 0755 "$DRIVE_DIR"
ls -ld "$DRIVE_DIR"

step "Step 9 of 11: the drive copy of the vault log, append-only"
if [ -L "$DRIVE_LOG" ] || { [ -e "$DRIVE_LOG" ] && [ ! -f "$DRIVE_LOG" ]; }; then
    red "$DRIVE_LOG exists but is not a plain file. Not touching it. Stop here and tell Odin."
    exit 1
fi
if [ -e "$DRIVE_LOG" ]; then
    echo "$DRIVE_LOG already exists — leaving it exactly as it is."
else
    echo "Copying $LOG to $DRIVE_LOG (holding the vault's lock, so no add runs half-way through)"
    flock -s "$LOG" install -o root -g root -m 0644 "$LOG" "$DRIVE_LOG"
    chattr +a "$DRIVE_LOG"
fi
ls -l "$DRIVE_LOG"
lsattr "$DRIVE_LOG"
if ! lsattr "$DRIVE_LOG" | cut -d' ' -f1 | grep -q a; then
    red "The append-only flag 'a' is NOT set on the drive copy. Stop here and tell Odin."
    exit 1
fi
echo "Lines: local $(wc -l < "$LOG"), drive $(wc -l < "$DRIVE_LOG")"

step "Step 10 of 11: check everything with helix-vault-verify"
set +e
"$VERIFY_DST"
RC=$?
set -e
if [ "$RC" -eq 0 ]; then
    echo "${GREEN}helix-vault-verify: all OK (exit 0).${OFF}"
else
    red "helix-vault-verify exited $RC. Tell Odin what it printed above."
fi

step "Step 11 of 11: done"
if [ "$REMOVED" -eq 1 ]; then
    echo "${BOLD}You removed groups just now: REBOOT (not just log out). Programs started before this —${OFF}"
    echo "${BOLD}including Helix — keep the old groups until they are restarted.${OFF}"
else
    echo "${BOLD}Done. No reboot needed this time. Tell Odin 'check the drive vault'.${OFF}"
fi
echo "${BOLD}Never type your password into Helix's chat.${OFF}"
