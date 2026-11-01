#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Install the wheel and run the README quickstart from outside the checkout.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$(mktemp -d)"
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install "$ROOT"
export PATH="$VENV/bin:$PATH"

cd /tmp
location="$(python3 -c 'import mailcrawl; print(mailcrawl.__file__)')"
printf '%s\n' "$location"
case "$location" in
  */site-packages/*) ;;
  *)
    echo "mailcrawl is not installed in site-packages" >&2
    exit 1
    ;;
esac

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

python3 "$ROOT/scripts/crawler-journey.py" --repo "$ROOT"
