#!/usr/bin/env bash
# Phase 1 proof. Temp HOME, no credentials, no network.
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
  testdata/fixtures/gog-export/native/gmail/acct_synthetic/labels.json \
  testdata/fixtures/gog-export/native/gmail/acct_synthetic/messages/index.jsonl \
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

EXPORT="${REPO_ROOT}/testdata/fixtures/gog-export/v0"
TAXONOMY="${REPO_ROOT}/testdata/catalogue/taxonomy.synthetic.json"
DATA_DIR="${HOME}/.local/share/mailcrawl"
DB="${DATA_DIR}/personal.db"
FIXTURE_BEFORE="${PROOF_DIR}/fixtures.sha256.before"
FIXTURE_AFTER="${PROOF_DIR}/fixtures.sha256.after"

record_fixture_hashes() {
  find "${REPO_ROOT}/testdata" -type f | sort | xargs sha256sum >"$1"
}

dump_hash() {
  (
    cd "${REPO_ROOT}"
    python3 - "$1" <<'PY'
import sys
from mailcrawl.store import connect, ordered_row_dump_hash

conn = connect(sys.argv[1])
try:
    print(ordered_row_dump_hash(conn))
finally:
    conn.close()
PY
  )
}

record_fixture_hashes "${FIXTURE_BEFORE}"

log "check: init creates only personal.db"
(
  cd "${REPO_ROOT}"
  python3 -m mailcrawl --json init --account pessoal --email ada@example.com
) | tee "${PROOF_DIR}/init.log"
[[ -f "${DB}" ]] || fail "personal.db missing"
[[ ! -e "${DATA_DIR}/index.db" ]] || fail "init created index.db"
[[ ! -e "${DATA_DIR}/reads.db" ]] || fail "init created reads.db"
[[ ! -e "${DATA_DIR}/pessoal.db" ]] || fail "init created pessoal.db"

log "check: import count matches the manifest"
import_out="$(
  cd "${REPO_ROOT}"
  python3 -m mailcrawl --json import gog-export \
    --account personal \
    --dir "${EXPORT}" \
    --taxonomy "${TAXONOMY}"
)"
echo "${import_out}" | tee "${PROOF_DIR}/import.log"
IMPORT_JSON="${import_out}" EXPORT_JSON="${EXPORT}/export.json" python3 <<'PY' || fail "import count does not match the manifest"
import json, os
doc = json.loads(os.environ["IMPORT_JSON"])
manifest = json.loads(open(os.environ["EXPORT_JSON"], encoding="utf-8").read())
want = manifest["example_manifest"]["messages"]
if doc["n_seen"] != want or doc["n_new"] != want:
    raise SystemExit(1)
PY

hash1="$(dump_hash "${DB}")"
log "check: reimport keeps the ordered-row-dump hash"
reimport_out="$(
  cd "${REPO_ROOT}"
  python3 -m mailcrawl --json import gog-export \
    --account personal \
    --dir "${EXPORT}" \
    --taxonomy "${TAXONOMY}"
)"
echo "${reimport_out}" | tee "${PROOF_DIR}/reimport.log"
hash2="$(dump_hash "${DB}")"
[[ "${hash1}" == "${hash2}" ]] || fail "reimport hash ${hash1} != ${hash2}"
REIMPORT_JSON="${reimport_out}" python3 <<'PY' || fail "reimport did not count every row as a duplicate"
import json, os
doc = json.loads(os.environ["REIMPORT_JSON"])
if doc["n_dup"] != doc["n_seen"] or doc["n_new"] != 0:
    raise SystemExit(1)
PY
echo "${hash1}" >"${PROOF_DIR}/row-dump.hash"
log "reimport hash stable: ${hash1}"

log "check: search reuniao finds reunião"
search_out="$(
  cd "${REPO_ROOT}"
  python3 -m mailcrawl --json search reuniao --account personal
)"
echo "${search_out}" | tee "${PROOF_DIR}/search.log"
SEARCH_JSON="${search_out}" python3 <<'PY' || fail "search reuniao missed reunião"
import json, os
doc = json.loads(os.environ["SEARCH_JSON"])
hits = doc.get("hits") or []
if not any("reunião" in (hit.get("snippet") or "") for hit in hits):
    raise SystemExit(1)
PY

log "check: native gog export imports without export.json"
NATIVE="${REPO_ROOT}/testdata/fixtures/gog-export/native"
(
  cd "${REPO_ROOT}"
  python3 -m mailcrawl --json init --account thehive --email ada@example.com
) | tee "${PROOF_DIR}/native-init.log"
native_out="$(
  cd "${REPO_ROOT}"
  python3 -m mailcrawl --json import gog-export \
    --account thehive \
    --dir "${NATIVE}" \
    --taxonomy "${TAXONOMY}"
)"
echo "${native_out}" | tee "${PROOF_DIR}/native-import.log"
NATIVE_JSON="${native_out}" python3 <<'PY' || fail "native import count is not 3"
import json, os
doc = json.loads(os.environ["NATIVE_JSON"])
if doc.get("n_seen") != 3 or doc.get("n_new") != 3 or doc.get("n_error") != 0:
    raise SystemExit(1)
PY
native_search="$(
  cd "${REPO_ROOT}"
  python3 -m mailcrawl --json search reuniao --account thehive
)"
echo "${native_search}" | tee "${PROOF_DIR}/native-search.log"
SEARCH_JSON="${native_search}" python3 <<'PY' || fail "native search reuniao missed reunião"
import json, os
doc = json.loads(os.environ["SEARCH_JSON"])
hits = doc.get("hits") or []
if not any("reunião" in (hit.get("snippet") or "") for hit in hits):
    raise SystemExit(1)
PY
native_again="$(
  cd "${REPO_ROOT}"
  python3 -m mailcrawl --json import gog-export \
    --account thehive \
    --dir "${NATIVE}"
)"
echo "${native_again}" | tee "${PROOF_DIR}/native-reimport.log"
REIMPORT_JSON="${native_again}" python3 <<'PY' || fail "native reimport was not a duplicate"
import json, os
doc = json.loads(os.environ["REIMPORT_JSON"])
if doc.get("n_dup") != 3 or doc.get("n_new") != 0:
    raise SystemExit(1)
PY

log "check: missing required keys fail closed"
for key in example_message_id example_thread_id example_label_ids example_date; do
  broken="$(mktemp -d)"
  KEY="${key}" SRC="${EXPORT}/export.json" DEST="${broken}" python3 <<'PY'
import json, os
from pathlib import Path
doc = json.loads(Path(os.environ["SRC"]).read_text(encoding="utf-8"))
del doc["example_messages"][0][os.environ["KEY"]]
Path(os.environ["DEST"], "export.json").write_text(json.dumps(doc), encoding="utf-8")
PY
  set +e
  bad_out="$(
    cd "${REPO_ROOT}"
    python3 -m mailcrawl --json import gog-export --account personal --dir "${broken}"
  )"
  bad_code=$?
  set -e
  rm -rf "${broken}"
  echo "${bad_out}" >>"${PROOF_DIR}/unsupported.log"
  [[ "${bad_code}" -eq 3 ]] || fail "missing ${key} exited ${bad_code}"
  printf '%s' "${bad_out}" | grep -q unsupported_schema || fail "missing ${key} lacked unsupported_schema"
done

record_fixture_hashes "${FIXTURE_AFTER}"
if ! diff -q "${FIXTURE_BEFORE}" "${FIXTURE_AFTER}" >/dev/null; then
  diff -u "${FIXTURE_BEFORE}" "${FIXTURE_AFTER}" | tee "${PROOF_DIR}/fixtures-diff.log" || true
  fail "source fixture hashes changed"
fi
log "source fixture hashes unchanged"

python3 - "${PROOF_DIR}/summary.json" "${hash1}" <<'PY'
import json
import sys

path, digest = sys.argv[1], sys.argv[2]
doc = {
    "schema": "mailcrawl.phase1-proof.v0",
    "status": "pass",
    "home_isolated": True,
    "network": "blocked",
    "credentials": "unset",
    "row_dump_hash": digest,
    "checks": [
        "hygiene",
        "fixtures",
        "secrets",
        "python_tests",
        "init_personal_db_only",
        "import_count_matches_manifest",
        "reimport_row_dump_hash",
        "search_reuniao",
        "native_gog_export",
        "missing_required_key_unsupported_schema",
        "fixture_sha256_unchanged",
    ],
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(doc, handle, indent=2)
    handle.write("\n")
PY

log "phase 1 proof passed"
