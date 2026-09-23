#!/usr/bin/env bash
# Install ytsum. Safe to re-run; it upgrades an existing install in place.
set -euo pipefail

REPO="https://github.com/quarksus/youtube-summarizer.git"
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/ytsum"
BIN_DIR="$HOME/.local/bin"
VENV="$PREFIX/venv"

step() { printf '\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
warn() { printf '\033[33m  %s\033[0m\n' "$*"; }
fail() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

step "Installing ytsum"

# --- 1. Python ---------------------------------------------------------------
command -v python3 >/dev/null 2>&1 || fail "python3 not found. Install Python 3.9 or newer, then re-run."
PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || fail "Python $PYVER is too old. ytsum needs 3.9 or newer."
note "Python $PYVER"

# --- 2. Virtual environment --------------------------------------------------
# Some distributions ship Python without ensurepip. Try hardest to avoid needing
# a password: plain venv, then a userspace pip bootstrap, and only then sudo.
mkdir -p "$PREFIX"
if ! python3 -m venv "$VENV" >/dev/null 2>&1; then
  note "This Python has no bundled pip; setting one up."
  rm -rf "$VENV"
  if python3 -m venv --without-pip "$VENV" >/dev/null 2>&1 \
     && curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$PREFIX/get-pip.py" \
     && "$VENV/bin/python" "$PREFIX/get-pip.py" --quiet >/dev/null 2>&1; then
    rm -f "$PREFIX/get-pip.py"
    note "pip installed without administrator rights"
  else
    rm -f "$PREFIX/get-pip.py"
    warn "Needs the system package python$PYVER-venv. You'll be asked for your password."
    if command -v apt-get >/dev/null 2>&1; then
      sudo apt-get update -qq && sudo apt-get install -y "python$PYVER-venv" || \
      sudo apt-get install -y python3-venv
    elif command -v dnf >/dev/null 2>&1; then
      sudo dnf install -y python3-virtualenv
    elif command -v pacman >/dev/null 2>&1; then
      sudo pacman -S --noconfirm python-virtualenv
    else
      fail "Couldn't detect your package manager. Install the python venv package, then re-run."
    fi
    rm -rf "$VENV"
    python3 -m venv "$VENV" || fail "Still couldn't create a virtual environment."
  fi
fi
note "Environment ready at $VENV"

# --- 3. ytsum and its dependencies -------------------------------------------
# Installing from a local checkout if there is one, otherwise straight from GitHub.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo .)"
if [ -f "$HERE/pyproject.toml" ]; then
  SOURCE="$HERE"
  note "Installing from this folder"
else
  SOURCE="git+$REPO"
  note "Downloading from GitHub"
fi
"$VENV/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
"$VENV/bin/pip" install --quiet --upgrade "$SOURCE" || fail "Installation failed."
note "Installed $("$VENV/bin/python" -c 'import yt_dlp, anthropic; print("anthropic", anthropic.__version__)' 2>/dev/null || echo "dependencies")"

# --- 4. Put it on the PATH ---------------------------------------------------
mkdir -p "$BIN_DIR"
ln -sf "$VENV/bin/ytsum" "$BIN_DIR/ytsum"
note "Command installed at $BIN_DIR/ytsum"

echo
case ":$PATH:" in
  *":$BIN_DIR:"*)
    step "Done. Run: ytsum"
    ;;
  *)
    step "Done - one more step"
    warn "$BIN_DIR isn't on your PATH yet. Add it with:"
    echo
    echo "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc && source ~/.bashrc"
    echo
    note "Or run it directly: $BIN_DIR/ytsum"
    ;;
esac
