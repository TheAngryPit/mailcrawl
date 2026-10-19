#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Install is the caller's job. This walks init through export from /tmp.
set -euo pipefail

cd /tmp

export http_proxy="${http_proxy:-http://127.0.0.1:9}"
export https_proxy="${https_proxy:-http://127.0.0.1:9}"
export HTTP_PROXY="$http_proxy"
export HTTPS_PROXY="$https_proxy"
export ALL_PROXY="${ALL_PROXY:-$http_proxy}"

if [[ -z "${REPO:-}" ]]; then
  echo "REPO is required" >&2
  exit 1
fi

python3 - << 'PY'
import socket
try:
    socket.create_connection(("1.1.1.1", 443), 3)
except OSError:
    raise SystemExit(0)
raise SystemExit("journey network is reachable")
PY

location="$(python3 -c 'import mailcrawl; print(mailcrawl.__file__)')"
printf '%s\n' "$location"
case "$location" in
  */site-packages/*) ;;
  *)
    echo "mailcrawl is not installed in site-packages" >&2
    exit 1
    ;;
esac

RULES="$(python3 -c 'from mailcrawl.rules import bundled_rules_path; print(bundled_rules_path())')"
NOW=1780272000
ACCOUNT=personal
ARCHIVE="$(mktemp -d /tmp/mailcrawl-e2e-archive.XXXXXX)"
EVIDENCE="$(mktemp -d /tmp/mailcrawl-e2e-evidence.XXXXXX)"
CORPUS="$(mktemp -d /tmp/mailcrawl-e2e-corpus.XXXXXX)"
ON="$EVIDENCE/on.toml"
OFF="$EVIDENCE/off.toml"

python3 "$REPO/scripts/e2e_corpus.py" --out "$CORPUS"

python3 -m mailcrawl --home "$ARCHIVE" --config "$ON" init --account "$ACCOUNT" --email ada@example.com

write_config() {
  local path="$1"
  local review="$2"
  cat > "$path" << EOF
[accounts.${ACCOUNT}]
email = "ada@example.com"
owner_addresses = ["ada@example.com", "ada.alias@example.net"]

[inference]
embeddings = true
embedder = "fake"
device = "cpu"

[rules]
path = "${RULES}"

[review]
llm_review = ${review}
EOF
}

write_config "$ON" true
write_config "$OFF" false

mc() {
  local name="$1"
  local cfg="$2"
  shift 2
  python3 -m mailcrawl --json --home "$ARCHIVE" --config "$cfg" "$@" > "$EVIDENCE/$name.json"
  python3 - "$EVIDENCE/$name.json" << 'PY'
import json
import sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
if doc.get("ok") is not True:
    raise SystemExit(doc)
PY
}

mc import-native "$ON" import gog-export --dir "$CORPUS/native" --account "$ACCOUNT"
mc import-mbox "$ON" import mbox "$CORPUS/corpus.mbox" --account "$ACCOUNT"
python3 - "$ARCHIVE" << 'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
outside = (
    Path("/tmp/mailcrawl-e2e-escape"),
    Path("/tmp/mailcrawl-e2e-escape.png"),
    Path("/tmp/mailcrawl-e2e-absolute.png"),
)
escaped = [str(path) for path in outside if path.exists()]
for path in root.rglob("*"):
    if "mailcrawl-e2e-escape" in path.name or "mailcrawl-e2e-absolute" in path.name:
        escaped.append(str(path))
images = [path for path in (root / "images").glob("*") if path.is_file()]
if not images:
    raise SystemExit("image attachment was not stored under the archive")
for path in images:
    name = path.name
    if len(name) != 64 or any(char not in "0123456789abcdef" for char in name):
        escaped.append(str(path))
if escaped:
    raise SystemExit("attachment name escaped the archive: " + ", ".join(escaped))
PY
mc from-me "$ON" from-me --account "$ACCOUNT" --apply
mc stats "$ON" stats --account "$ACCOUNT"
mc rules "$ON" rules --account "$ACCOUNT" --now "$NOW"
mc embed "$ON" embed --account "$ACCOUNT" --embedder fake
mc knn "$ON" knn --account "$ACCOUNT" --embedder fake --now "$NOW"
mc reevaluate "$ON" rules --account "$ACCOUNT" --reevaluate --apply --csv "$EVIDENCE/reevaluate.csv" --now "$NOW"
mc queue-off "$OFF" review queue --account "$ACCOUNT" --now "$NOW"
mc queue-on "$ON" review queue --account "$ACCOUNT" --now "$NOW"
python3 "$REPO/scripts/e2e_harness.py" \
  --queue "$EVIDENCE/queue-off.json" \
  --table "$CORPUS/harness.json" \
  --out "$EVIDENCE/decide.jsonl"
mc decide "$ON" decide --account "$ACCOUNT" --file "$EVIDENCE/decide.jsonl" --now "$NOW"
mc hold "$OFF" rules --account "$ACCOUNT" --reevaluate --apply --csv "$EVIDENCE/hold.csv" --now "$NOW"
mc knn-again "$ON" knn --account "$ACCOUNT" --embedder fake --now "$NOW"
cp "$ARCHIVE/${ACCOUNT}.db" "$EVIDENCE/revived.db"
mc undo "$ON" review undo --account "$ACCOUNT" --thread e2e-en-old-ask --now "$NOW"
mc queue-off-after "$OFF" review queue --account "$ACCOUNT" --now "$NOW"
mc queue-on-after "$ON" review queue --account "$ACCOUNT" --now "$NOW"
mc export "$ON" dry-run --account "$ACCOUNT" --export-decisions "$EVIDENCE/export.csv" --now "$NOW"

record=()
if [[ "${MAILCRAWL_E2E_RECORD:-}" == "1" ]]; then
  record=(--record)
fi
python3 "$REPO/scripts/e2e_check.py" \
  --db "$ARCHIVE/${ACCOUNT}.db" \
  --revived-db "$EVIDENCE/revived.db" \
  --queue-off "$EVIDENCE/queue-off.json" \
  --queue-on "$EVIDENCE/queue-on.json" \
  --queue-off-after-undo "$EVIDENCE/queue-off-after.json" \
  --queue-on-after-undo "$EVIDENCE/queue-on-after.json" \
  --export "$EVIDENCE/export.csv" \
  --native-import "$EVIDENCE/import-native.json" \
  --mbox-import "$EVIDENCE/import-mbox.json" \
  --expected "$REPO/testdata/e2e/expected.json" \
  ${record[@]+"${record[@]}"}

printf 'e2e archive %s\n' "$ARCHIVE"
printf 'e2e evidence %s\n' "$EVIDENCE"
