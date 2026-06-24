#!/usr/bin/env bash
# macstats — installer
# Builds the HID sensor helper, installs Python deps, and links `macstat`
# into ~/.local/bin so it's runnable from anywhere.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
PY="${PYTHON:-python3}"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }

bold "==> Checking platform"
if [[ "$(uname)" != "Darwin" ]]; then
  echo "macstats is macOS-only." >&2
  exit 1
fi
ok "$(uname) $(uname -m)"

bold "==> Checking Python"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "python3 not found. Install Python 3.10+ first." >&2
  exit 1
fi
ok "$($PY --version)"

bold "==> Installing Python dependencies"
"$PY" -m pip install --user -r "$SCRIPT_DIR/requirements.txt" >/dev/null
ok "psutil, rich installed (--user)"

bold "==> Building macstat-sensors"
(cd "$SCRIPT_DIR" && make -s)
ok "compiled $SCRIPT_DIR/macstat-sensors"

bold "==> Linking command"
mkdir -p "$BIN_DIR"
ln -sf "$SCRIPT_DIR/macstat.py" "$BIN_DIR/macstat"
chmod +x "$SCRIPT_DIR/macstat.py"
ok "$BIN_DIR/macstat -> $SCRIPT_DIR/macstat.py"

# PATH check
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  warn "$BIN_DIR is not in your PATH."
  echo "    Add this to your shell rc file (~/.zshrc):"
  echo "      export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

bold "==> Done"
cat <<EOF

Run with:   macstat   (Ctrl-C to exit)

Optional — to enable the Power & Thermal panel's wattage data,
allow powermetrics to run without password:

  echo "\$(whoami) ALL=(ALL) NOPASSWD: /usr/bin/powermetrics" \\
    | sudo tee /etc/sudoers.d/powermetrics
  sudo chmod 440 /etc/sudoers.d/powermetrics
  sudo visudo -c -f /etc/sudoers.d/powermetrics

Without it, CPU/GPU/ANE/SoC wattage and thermal pressure won't show,
but temperatures and everything else still work.
EOF
