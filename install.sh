#!/usr/bin/env bash
# Helix installer. Puts Helix in ~/helix, fills in this machine's paths, sets up Python,
# and asks a few questions so Helix starts out knowing who you are. Nothing is sent anywhere.
#
#   bash install.sh            interactive
#   HELIX_NO_VENV=1 ...        skip creating the Python environment (if you manage it yourself)
set -euo pipefail
KIT="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
DEST="$HOME/helix"
BULK_DEFAULT="$HOME/helix_bulk"

say(){ printf '\n\033[1m%s\033[0m\n' "$*"; }
ask(){ local q="$1" d="${2:-}" a; read -r -p "$q${d:+ [$d]}: " a || true; echo "${a:-$d}"; }

say "Helix installer"
[ "$(uname -s)" = Linux ] || { echo "Helix is built for Linux (tested on Ubuntu/Xubuntu)."; exit 1; }
command -v python3 >/dev/null || { echo "python3 is missing: sudo apt install python3 python3-venv"; exit 1; }
mkvenv(){  # python3 -m venv needs Ubuntu's python3-venv package; virtualenv works without it
  if python3 -c "import ensurepip" 2>/dev/null; then python3 -m venv "$1"
  elif command -v virtualenv >/dev/null; then virtualenv -q "$1"
  else echo "Cannot create a Python environment. Run: sudo apt install python3-venv   then run this installer again."; exit 1; fi; }
if [ "${HELIX_NO_VENV:-0}" != 1 ] && ! python3 -c "import ensurepip" 2>/dev/null && ! command -v virtualenv >/dev/null; then
  echo "Missing Ubuntu package python3-venv. Run: sudo apt install python3-venv   then run this installer again."; exit 1
fi
if ! command -v claude >/dev/null; then
  echo "Claude Code is not installed. Install it first (https://docs.anthropic.com/claude-code), log in once"
  echo "by running 'claude' in a terminal, then run this installer again."; exit 1
fi
git config user.name >/dev/null 2>&1 || echo "note: git has no user.name set. Some self-checks commit to temporary repos; run: git config --global user.name \"Your Name\"; git config --global user.email you@example.com"
command -v bwrap >/dev/null || echo "note: 'bubblewrap' is missing (sudo apt install bubblewrap). The write wall needs it."
[ -e "$DEST" ] && { echo "$DEST already exists. Move it away first; this installer never overwrites."; exit 1; }

say "A few questions, so Helix knows who it is working for (you can change all of it later in ~/helix/memory/BRAIN.md)"
NAME="$(ask "Your name")"
CALL="$(ask "What should Helix call you" "$NAME")"
ABOUT="$(ask "One or two lines about you (work, interests, how you like answers)")"
LANG_PREF="$(ask "Which language should Helix answer in" "English")"
BULK="$(ask "Folder for big files (models, datasets). Use a large disk if you have one" "$BULK_DEFAULT")"
SEPARATE="$(ask "Is that big-files folder on its own separate disk? (y/N)" "N")"
VAULT="$(ask "Optional: a folder on a second/external drive for the tamper-evident copy (leave empty to skip)" "")"

say "Copying Helix to $DEST"
cp -a "$KIT/helix" "$DEST"
mkdir -p "$BULK"
python3 - "$DEST" "$HOME" "$USER" "$BULK" "${VAULT:-$BULK/vault_copy}" <<'PY'
import sys, pathlib, re
dest, home, user, bulk, vault = sys.argv[1:]
import secrets
rep = {"__HOME__": home, "__USER__": user, "__BULK__": bulk, "__VAULT_DRIVE__": vault, "__MEDIA__": f"/media/{user}", "__TOKEN__": secrets.token_hex(16)}
n = 0
for p in pathlib.Path(dest).rglob("*"):
    if not p.is_file() or p.suffix in (".npy", ".onnx", ".bin"): continue
    try: s = p.read_text()
    except UnicodeDecodeError: continue
    t = s
    for k, v in rep.items(): t = t.replace(k, v)
    if t != s: p.write_text(t); n += 1
print(f"  filled in this machine's paths in {n} files")
PY

case "$SEPARATE" in [yY]*) SEP=true ;; *) SEP=false ;; esac
printf '{"bulk_is_separate_disk": %s, "vault_drive": "%s"}\n' "$SEP" "$VAULT" > "$DEST/memory/machine.json"
mkdir -p "$DEST/workspace/seam/registry"
[ -e "$DEST/workspace/seam/registry/registry.json" ] || echo '{}' > "$DEST/workspace/seam/registry/registry.json"
touch "$DEST/workspace/seam/registry/REGATE_LEDGER.jsonl"   # empty ledger: regate `check` says OK on an empty registry

say "Writing your starting memory"
python3 - "$DEST" "$NAME" "$CALL" "$ABOUT" "$LANG_PREF" "$(hostname)" "$BULK" "$KIT/templates" <<'PY'
import sys, pathlib, shutil, datetime
dest, name, call, about, lang, host, bulk, tpl = sys.argv[1:]
d = pathlib.Path(dest); T = pathlib.Path(tpl)
today = datetime.date.today().isoformat()
fill = lambda s: (s.replace("{NAME}", name or "the user").replace("{CALL}", call or name or "you")
                   .replace("{ABOUT}", about or "(not filled in yet: tell Helix about yourself and ask it to save it)")
                   .replace("{LANG}", lang).replace("{HOST}", host).replace("{HOME}", str(pathlib.Path.home()))
                   .replace("{BULK}", bulk).replace("{TODAY}", today))
(d/"memory/BRAIN.md").write_text(fill((T/"BRAIN.md").read_text()))
(d/"memory/ALICE.md").write_text(fill((T/"ALICE.md").read_text()))
(d/"memory/PINNED.txt").write_text((T/"PINNED.txt").read_text())
(d/"memory/schedules.json").write_text((T/"schedules.json").read_text())
for f in (T/"facts").glob("*.md"): (d/"memory/facts"/f.name).write_text(fill(f.read_text()))
colab = pathlib.Path.home()/"colab"; colab.mkdir(exist_ok=True)
if not (colab/"COLAB.md").exists(): (colab/"COLAB.md").write_text(fill((T/"COLAB.md").read_text()))
print("  memory/BRAIN.md, ALICE.md, PINNED.txt and", len(list((T/"facts").glob("*.md"))), "starter facts written")
PY

say "Starting the fact gate's ledger (it records the starter facts so it can tell later changes apart)"
python3 "$DEST/workspace/factgate/factgate.py" baseline "$DEST/memory/facts" | tail -2

if [ "${HELIX_NO_VENV:-0}" != 1 ]; then
  say "Setting up Python (a few minutes)"
  mkvenv "$DEST/.venv"
  "$DEST/.venv/bin/pip" install -q --upgrade pip
  "$DEST/.venv/bin/pip" install -q -r "$KIT/requirements.txt"
  mkvenv "$BULK/venvs/lint" && echo "pyflakes==4.0.2" > "$BULK/venvs/lint/requirements.txt" \
    && "$BULK/venvs/lint/bin/pip" install -q -r "$BULK/venvs/lint/requirements.txt"   # the build check's linter
  echo "  core packages installed. Optional extras (speech-to-text, image generation): see requirements-optional.txt"
fi
say "Downloading the small memory-search model (bge-small, about 130 MB, CPU only)"
M="$BULK/memretrieve/model/bge-small-en-v1.5"; REV=5c38ec7c405ec4b44b94cc5a9bb96e735b38267a
mkdir -p "$M/onnx"
curl -sSL -o "$M/onnx/model.onnx" "https://huggingface.co/BAAI/bge-small-en-v1.5/resolve/$REV/onnx/model.onnx" \
 && curl -sSL -o "$M/tokenizer.json" "https://huggingface.co/BAAI/bge-small-en-v1.5/resolve/$REV/tokenizer.json" \
 && echo "828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35  $M/onnx/model.onnx" | sha256sum -c --quiet \
 && echo "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66  $M/tokenizer.json" | sha256sum -c --quiet \
 && echo "  model downloaded and checked" || echo "  model download FAILED: memory search will use keywords only until it is fixed"

say "Giving each module its own git history (builds commit their work on branches)"
for d in "$DEST"/workspace/*/ "$DEST"/build_orders "$DEST"/studio; do
  ( cd "$d" && git init -q && git add -A && git -c user.name=helix -c user.email=helix@localhost commit -q -m "Helix kit: initial" ) || true
done
chmod +x "$DEST"/*.sh "$DEST"/build_orders/*.sh 2>/dev/null || true

say "Done."
cat <<EOF
Next:
  1. Optional voice: download kokoro-v1.0.onnx and voices-v1.0.bin into ~/helix/models/kokoro/
     (https://github.com/thewh1teagle/kokoro-onnx/releases). Without them Helix is text-only.
  2. Optional second seat (Alice): install the Gemini CLI as 'agy' and log in. Without it Odin works alone.
  3. Start Helix:   ~/helix/start_helix.sh     (opens http://127.0.0.1:7777)
  4. Better still: open Claude Code in this kit folder and say
     "Read BLUEPRINT.md and finish setting up Helix with me." It will check every step.
EOF
