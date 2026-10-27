# helix-machine — what Helix may do to this computer as root (ORDER 67)

Helix runs as `__USER__`. It can read most of the machine's health on its own (disks, CPU
temperature, the GPU, failed services, pending updates). For the few things that need root, there is
ONE program, `/usr/local/sbin/helix-machine`, and ONE sudoers line that lets Helix run it without a
password. Helix only calls it from the 04:30 nightly.

## What it can do (the five verbs, nothing else)
| verb | what it does |
|---|---|
| `health` | reads the SMART health of each disk (`smartctl -H -A`); changes nothing |
| `update` | `apt-get update`, then `apt-get upgrade` (never takes a package off); at most 45 min; refuses if another install is running |
| `clean` | empties apt's download cache; trims the system journal to 500 MB |
| `restart-sidekick` | restarts `sidekick.service` |
| `reboot` | reboots in 2 minutes, but ONLY if the machine itself says it must (`/var/run/reboot-required`, or the GPU driver mismatch). Helix cannot invent a reason. |

Every call is logged in `/var/log/helix-machine.log` (time, verb, exit code).

## What it can't do
Everything else. No other command, no file paths, no options, no shell. One word in, or it refuses.
It never removes packages: when some are no longer needed the nightly tells you to run
`sudo apt autoremove` yourself.

## The night reboot and automatic login
A reboot is only needed after some updates or a GPU driver change (on 2026-10-17 a driver update broke
`nvidia-smi` until a reboot). Helix reboots only between 04:00 and 06:59, after the nightly, and only if
**automatic login** is on, because Helix starts from your desktop session: without automatic login it
would stay off until you log in. Then it just reports "reboot due" and leaves it to you.

The cost of automatic login: anyone who switches the PC on is logged in as you. The installer asks.

## Install / remove
- Install: `sudo bash ~/helix/machine/INSTALL_FOR_USER.sh` (compare the fingerprint with ORDER_67_report.md).
- Remove: `sudo bash ~/helix/machine/UNINSTALL_FOR_USER.sh` (keeps the log).
