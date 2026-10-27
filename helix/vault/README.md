# The vault (ORDER 19)

**What it is.** A log at `/var/lib/helix-vault/vault.log`, owned by root and set append-only
(`chattr +a`): lines can be added, never changed or removed. Each line records, for the three ledgers
(FACTS, REGATE, CATCHES), how many lines each had and a sha256 of those lines. Root measures the files
itself; the caller only gives names. Every line also carries the hash of the line before it (`prev`, sha256
of that line's bytes including its newline), so the log is a chain. `seq` counts from 1.

**Programs.** `helix-vault-add [names]` (root, via sudo) adds one line. It refuses if any ledger has shrunk
or changed lines that an earlier anchor recorded. `helix-vault-verify` (anyone) checks the chain, and
checks every anchor, oldest first, against the live ledgers.

**What the user does.** Read `helix-vault-add` (under 210 lines). Run `sudo bash INSTALL_FOR_USER.sh` once in
a terminal, compare the fingerprints, answer the group question, then **reboot** (running programs,
Helix included, keep old groups until then). To remove it, run `UNINSTALL_FOR_USER.sh` (the log stays).

**What Odin can do afterwards:** run `sudo -n /usr/local/sbin/helix-vault-add` (names only) and
`helix-vault-verify`. **What Odin cannot do:** edit, shorten or delete the vault log; change either
program; run anything else as root without the user's password; choose what hash gets recorded.

## Honest limits
- The vault proves a ledger was not rewritten **after** an anchor. Lines appended between two anchors can
  be rewritten before the next anchor. Anchoring often (order 20: nightly, and optionally after each
  build) shrinks that window. It cannot close it.
- It proves the record was not edited, **not that it is right.**
- If root on BOX1 is lost, both copies are lost (see the drive copy, below).
- If a ledger ever has to be rewritten for a good reason, the vault will say REWRITTEN from then on. That
  is correct. the user decides what to do, never Odin.
- The chain catches an edited earlier line, but not a change to the newest line alone. The `+a` flag is
  what protects the newest line.
- The wall is sudo and root. If Odin's user gets root another way (a group like docker, or a password
  typed into chat), the vault can be rebuilt. Hence step 5 of the install.
- A checker reading a file the checked party controls can be shown different bytes at different times.
  Order 20 must take ONE snapshot of each ledger and run both `factgate check` and the vault comparison
  over the same bytes.

**If the log's last line is ever half-written** (crash mid-write): only the user fixes it, at the desk:
`sudo chattr -a`, remove the partial last line, `sudo chattr +a`. Odin never does this and cannot.

## The drive copy (ORDER 20)
After each local append, `helix-vault-add` brings `__VAULT_DRIVE__/helix-vault/vault.log` (root,
0644, `+a`, on the external `odin` drive) up to the local log, while it still holds the lock. The drive copy must be a
byte prefix of the local log. If it is behind (the drive was unplugged), the missing lines are appended. If
it differs, it is **never repaired**. The add creates nothing on the drive: the user's install does that once.
Exit 6 = anchored locally, drive copy not updated (`MISSING` / `UNSAFE` / `DIVERGED`). `helix-vault-verify`
checks the drive copy too, and fails on MISSING. `--no-drive` skips that check. `--log <drive path> --no-drive` verifies
the drive copy on its own: it is a full vault.
- **Buys:** a second copy that survives the SSD dying or the OS being reinstalled, sits on a disk the user
  can unplug and carry, and that Odin's user still cannot write (root-owned, `+a`).
- **Does not buy:** protection against root on this machine, which can rewrite both copies. Nor against
  fire or theft of the whole desk. **Unplugging the drive now and then and keeping it elsewhere turns
  it into a real outside witness.** The nightly says MISSING while it is away. That is expected.
- Odin owns the drive's top folder, so he can **rename or delete** the vault folder on it. He cannot make
  a root-owned replacement, so that shows as MISSING or UNSAFE every night, never as a silent pass.
