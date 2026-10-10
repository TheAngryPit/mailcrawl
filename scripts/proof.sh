#!/usr/bin/env bash
# Scaffold proof. Temp HOME, no credentials, no network.
# Does not import mail, build FTS, or run a product CLI.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROOF_DIR="${REPO_ROOT}/proof"
ORIG_HOME="${HOME}"
PROOF_HOME="$(mktemp -d)"
export HOME="${PROOF_HOME}"
export http_proxy="http://127.0.0.1:9"
export https_proxy="http://127.0.0.1:9"
export HTTP_PROXY="http://127.0.0.1:9"
export HTTPS_PROXY="http://127.0.0.1:9"
export ALL_PROXY="http://127.0.0.1:9"
unset GOG_KEY OPENAI_API_KEY ANTHROPIC_API_KEY GH_TOKEN GITHUB_TOKEN GOOGLE_APPLICATION_CREDENTIALS || true

mkdir -p "${PROOF_DIR}"

log() {
  echo "[proof] $*" | tee -a "${PROOF_DIR}/proof.log"
}

fail() {
  log "FAIL: $*"
  exit 1
}

cleanup() {
  rm -rf "${PROOF_HOME}"
}
trap cleanup EXIT

: >"${PROOF_DIR}/proof.log"

[[ "${HOME}" != "${ORIG_HOME}" ]] || fail "HOME was not isolated"
[[ "${HOME}" == "${PROOF_HOME}" ]] || fail "HOME is not the temp dir"

log "check: required synthetic fixtures"
for f in \
  testdata/fixtures/gog-export/v0/export.json \
  testdata/fixtures/gog-export/v0/note.eml \
  testdata/fixtures/gog-export/unknown/export.json \
  testdata/catalogue/taxonomy.synthetic.json
do
  [[ -f "${REPO_ROOT}/${f}" ]] || fail "missing fixture ${f}"
done

log "check: fixtures are synthetic"
python3 - "${REPO_ROOT}" <<'PY'
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
email = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+)")
bad = []
for path in root.joinpath("testdata").rglob("*"):
    if not path.is_file():
        continue
    text = path.read_text(encoding="utf-8")
    for match in email.finditer(text):
        if match.group(1).lower() != "example.com":
            bad.append(f"{path}: {match.group(0)}")
if bad:
    print("\n".join(bad))
    sys.exit(1)

v0 = json.loads((root / "testdata/fixtures/gog-export/v0/export.json").read_text())
if v0.get("schema") != "mailcrawl.gog-export.v0":
    sys.exit("v0 fixture schema drifted")
if v0.get("synthetic") is not True:
    sys.exit("v0 fixture is not marked synthetic")
allowed = {
    "schema",
    "synthetic",
    "note",
    "example_manifest",
    "example_labels",
    "example_messages",
}
if set(v0) != allowed:
    sys.exit(f"v0 keys drifted: {sorted(set(v0) ^ allowed)}")
required = {
    "example_message_id",
    "example_thread_id",
    "example_label_ids",
    "example_date",
}
for message in v0["example_messages"]:
    missing = required - set(message)
    if missing:
        sys.exit(f"v0 message missing {sorted(missing)}")
PY

log "check: repo hygiene"
"${REPO_ROOT}/scripts/hygiene.sh" | tee -a "${PROOF_DIR}/hygiene.log"

log "check: python tests"
(
  cd "${REPO_ROOT}"
  python3 -m unittest discover -s tests -v
) | tee -a "${PROOF_DIR}/tests.log"

python3 - "${PROOF_DIR}/summary.json" <<'PY'
import json
import sys

path = sys.argv[1]
doc = {
    "schema": "mailcrawl.scaffold-proof.v0",
    "status": "pass",
    "home_isolated": True,
    "network": "blocked",
    "credentials": "unset",
    "checks": ["hygiene", "fixtures", "secrets", "python_tests"],
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(doc, handle, indent=2)
    handle.write("\n")
PY

log "scaffold proof passed"
