#!/usr/bin/env bash
# Install the wheel and run the README quickstart from outside the checkout.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$(mktemp -d)"
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install "$ROOT"
export PATH="$VENV/bin:$PATH"

cd /tmp
python3 -c 'import mailcrawl; print(mailcrawl.__file__)'

export ARCHIVE="$(mktemp -d)"
export EXPORT="$ROOT/testdata/fixtures/gog-export/native"
block="$(awk '
  /^## Quickstart$/ { grab = 1; next }
  grab && /^```$/ { fence++; if (fence == 2) exit; next }
  grab && fence == 1 { print }
' "$ROOT/README.md")"
if [[ -z "$block" ]]; then
  echo "README quickstart block is missing" >&2
  exit 1
fi
printf '%s\n' "$block"
bash -euo pipefail -c "$block"
