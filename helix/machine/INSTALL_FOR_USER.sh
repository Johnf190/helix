#!/bin/bash
# INSTALL_FOR_USER.sh — lets Helix look after this machine (ORDER 67, item 32).
# Run it ONCE in a terminal, as:
#     sudo bash ~/helix/machine/INSTALL_FOR_USER.sh
# It prints every step before doing it and stops at the first error.
# Running it again is safe: steps that are already done are left as they are.
set -euo pipefail

USER_NAME="__USER__"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DST="/usr/local/sbin/helix-machine"
STATE_DIR="/var/lib/helix-machine"
LOG="/var/log/helix-machine.log"
SUDOERS="/etc/sudoers.d/helix-machine"
RULE="$USER_NAME ALL=(root) NOPASSWD: $DST"
GDM="/etc/gdm3/custom.conf"
GDM_BAK="/etc/gdm3/custom.conf.bak-helix"
RED=$'\e[1;31m'; GREEN=$'\e[1;32m'; BOLD=$'\e[1m'; OFF=$'\e[0m'

step() { echo; echo "${BOLD}=== $* ===${OFF}"; }
red()  { echo "${RED}$*${OFF}"; }

if [ "$(id -u)" -ne 0 ]; then
    red "This must be run with sudo:  sudo bash $HERE/INSTALL_FOR_USER.sh"
    exit 1
fi

# Copy the program into a root-only folder first. Everything below hashes and installs this
# copy, so nobody can swap the file in $HERE while this script waits for you.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp "$HERE/helix-machine" "$STAGE/helix-machine"

step "Step 1 of 6: the fingerprint of the program"
sha256sum "$STAGE/helix-machine"
echo "Compare this with the box at the top of ~/helix/build_orders/reports/ORDER_67_report.md."
echo "If it does NOT match exactly, press Ctrl+C now and tell Odin."
read -r -p "If it matches, press Enter to continue... "

step "Step 2 of 6: install the program (owned by root, nobody else can change it)"
install -o root -g root -m 0755 "$STAGE/helix-machine" "$DST"
install -d -o root -g root -m 0755 "$STATE_DIR"
if [ -e "$LOG" ]; then
    echo "$LOG already exists — leaving it exactly as it is."
else
    install -o root -g root -m 0644 /dev/null "$LOG"
    echo "Created empty $LOG"
fi
ls -l "$DST" "$LOG"
ls -ld "$STATE_DIR"

step "Step 3 of 6: allow $USER_NAME to run ONLY helix-machine as root, with no password"
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

step "Step 4 of 6: automatic login (your choice)"
echo "Without automatic login, after a night reboot nobody is logged in, so Helix stays off until you log in."
echo "So without it Helix will NOT reboot on its own; it only reports 'reboot due' in the morning."
echo "The cost of saying yes: anyone who switches this PC on is logged in as you ($USER_NAME)."
read -r -p "Turn on automatic login for $USER_NAME? [y/N] " ans
if [ "${ans:-}" = "y" ] || [ "${ans:-}" = "Y" ]; then
    if [ ! -f "$GDM" ]; then
        red "$GDM does not exist. Automatic login NOT changed. Tell Odin."
    else
        if [ -e "$GDM_BAK" ]; then
            echo "$GDM_BAK already exists (an earlier run) — keeping that original backup."
        else
            cp -p "$GDM" "$GDM_BAK"
            echo "Backed up $GDM to $GDM_BAK"
        fi
        TMP="$(mktemp)"
        # Edit only the two keys under [daemon]: drop any old copies of them, add them right
        # after the [daemon] header. Every other line stays exactly as it was.
        awk -v user="$USER_NAME" '
            /^[[:space:]]*\[/ { sect = $0; gsub(/[[:space:]]/, "", sect) }
            sect == "[daemon]" && /^[[:space:]]*AutomaticLogin(Enable)?[[:space:]]*=/ { next }
            { print }
            sect == "[daemon]" && !done && /^[[:space:]]*\[daemon\][[:space:]]*$/ {
                print "AutomaticLoginEnable=true"; print "AutomaticLogin=" user; done = 1 }
            END { if (!done) { print "[daemon]"; print "AutomaticLoginEnable=true"; print "AutomaticLogin=" user } }
        ' "$GDM" > "$TMP"
        install -o root -g root -m 0644 "$TMP" "$GDM"
        rm -f "$TMP"
        echo "Now in $GDM:"
        grep -n -A3 '^\[daemon\]' "$GDM"
        echo "${GREEN}Automatic login is on from the next boot.${OFF}"
    fi
else
    echo "Automatic login NOT turned on. Helix will report 'reboot due' and leave the reboot to you."
fi

step "Step 5 of 6: the sudo picture — please read it"
echo "--- sudo -l -U $USER_NAME ---"
sudo -l -U "$USER_NAME" || true
echo
echo "--- every NOPASSWD line (expected: exactly helix-vault-add and helix-machine) ---"
grep -Hn NOPASSWD /etc/sudoers.d/* /etc/sudoers 2>/dev/null | while IFS= read -r l; do
    case "$l" in
        *": $USER_NAME ALL=(root) NOPASSWD: /usr/local/sbin/helix-vault-add"|*": $RULE") echo "$l" ;;
        *) red "UNEXPECTED (not changed — tell Odin): $l" ;;
    esac
done

step "Step 6 of 6: done"
echo "${BOLD}Done. Tell Odin 'check the machine'.${OFF}"
echo "${BOLD}Never type your password into Helix's chat.${OFF}"
