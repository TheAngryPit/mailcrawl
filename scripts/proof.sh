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
  testdata/fixtures/mbox/takeout.mbox \
  testdata/fixtures/mbox/gog-after/gmail/acct_synthetic/labels.json \
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

log "check: cleaned body is indexed and the raw quote is not"
quoted_out="$(
  cd "${REPO_ROOT}"
  python3 -m mailcrawl --json search quoted --account personal
)"
echo "${quoted_out}" | tee "${PROOF_DIR}/search-quoted.log"
fake_out="$(
  cd "${REPO_ROOT}"
  python3 -m mailcrawl --json search fake --account personal
)"
echo "${fake_out}" | tee "${PROOF_DIR}/search-fake.log"
DB_PATH="${DB}" QUOTED_JSON="${quoted_out}" FAKE_JSON="${fake_out}" python3 <<'PY' || fail "cleaned body still indexes the quote"
import json, os, sqlite3
quoted = json.loads(os.environ["QUOTED_JSON"])
fake = json.loads(os.environ["FAKE_JSON"])
if quoted.get("hits"):
    raise SystemExit("quoted search returned hits")
ids = [hit.get("gmail_message_id") for hit in fake.get("hits") or []]
if "fixture-001" not in ids:
    raise SystemExit("fake search missed fixture-001")
conn = sqlite3.connect(os.environ["DB_PATH"])
try:
    raw, body = conn.execute(
        "SELECT body_text_raw, body_text FROM messages WHERE gmail_message_id = 'fixture-001'"
    ).fetchone()
    if "> quoted line stays" not in raw or "-- \nAda Fixture" not in raw:
        raise SystemExit("raw body dropped the quote or signature")
    if body != "This body is fake fixture text.":
        raise SystemExit("cleaned body is not the kept sentence")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != 3:
        raise SystemExit(f"user_version {version}")
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for table in ("decisions", "sender_decisions", "thread_mutes"):
        if table not in names:
            raise SystemExit(f"missing {table}")
    for banned in ("plans", "proposals", "embeddings", "clips"):
        if banned in names:
            raise SystemExit(f"unexpected {banned}")
finally:
    conn.close()
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

log "check: attachment parser skips missing bytes and unknown types"
PERSONAL_DB="${DB}" HIVE_DB="${DATA_DIR}/thehive.db" python3 - <<'PY' || fail "attachment parser statuses drifted"
import os, sqlite3
from mailcrawl.extract import BackendRead, parse_attachment

personal = sqlite3.connect(os.environ["PERSONAL_DB"])
try:
    row = personal.execute(
        "SELECT extract_status, extract_method, file_sha256 FROM attachments"
    ).fetchone()
finally:
    personal.close()
if row != ("skipped", "no_bytes", None):
    raise SystemExit(f"v0 attachment {row}")

hive = sqlite3.connect(os.environ["HIVE_DB"])
try:
    rows = {
        name: (status, method, digest, text)
        for name, status, method, digest, text in hive.execute(
            """
            SELECT filename, extract_status, extract_method, file_sha256, extracted_text
            FROM attachments
            """
        )
    }
    marker = hive.execute(
        "SELECT count(*) FROM msg_fts WHERE msg_fts MATCH 'ZZBINMARKERZZ'"
    ).fetchone()[0]
finally:
    hive.close()
if rows["note.txt"][0] != "ok" or "attachmenttoken" not in (rows["note.txt"][3] or ""):
    raise SystemExit(f"text attachment {rows.get('note.txt')}")
if rows["blob.bin"][0] != "unsupported" or rows["blob.bin"][3] is not None:
    raise SystemExit(f"binary attachment {rows.get('blob.bin')}")
if not rows["note.txt"][2] or not rows["blob.bin"][2]:
    raise SystemExit("missing file_sha256")
if marker != 0:
    raise SystemExit("binary bytes were indexed")

class Present:
    def available(self, kind: str) -> bool:
        return True
    def read(self, kind: str, payload: bytes) -> BackendRead:
        return BackendRead("x" * 24, 1, 59.0)

low = parse_attachment("application/pdf", b"%PDF", backend=Present())
if low.ocr_status != "low_confidence" or low.extract_status != "ok":
    raise SystemExit(f"threshold {low}")
missing = parse_attachment("application/pdf", b"%PDF")
if missing.extract_status != "skipped" or missing.ocr_status != "not_needed":
    raise SystemExit(f"default backend {missing}")
if missing.extract_method != "pdf":
    raise SystemExit(f"default method {missing.extract_method}")
PY

log "check: rules print counts and no message text"
(
  cd "${REPO_ROOT}"
  python3 -m mailcrawl --json init --account vcl --email ada@example.com
) >/dev/null
VCL_DB="${DATA_DIR}/vcl.db"
VCL_DB="${VCL_DB}" python3 - <<'PY'
import os
import sqlite3

conn = sqlite3.connect(os.environ["VCL_DB"])
conn.execute("PRAGMA foreign_keys = ON")
account = conn.execute("SELECT account_id FROM accounts").fetchone()[0]
now = 1700000000

def add(number: int, body: str, starred: bool) -> None:
    conn.execute(
        "INSERT INTO senders(account_id, address_norm, domain) VALUES (?, ?, 'example.com')",
        (account, f"bao{number}@example.com"),
    )
    sender = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO threads(account_id, gmail_thread_id) VALUES (?, ?)",
        (account, f"rules-{number}"),
    )
    thread = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO messages(
          account_id, gmail_message_id, thread_pk, internal_date, sender_id,
          body_text, is_from_me, raw_sha256, provenance_hash, imported_at
        ) VALUES (?, ?, ?, ?, ?, ?, 0, 'raw', 'prov', ?)
        """,
        (account, f"rules-msg-{number}", thread, now - 86400, sender, body, now),
    )
    if starred:
        msg = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """
            INSERT INTO labels(account_id, gmail_label_id, name, kind, seen_at)
            VALUES (?, 'STARRED', 'STARRED', 'system', ?)
            """,
            (account, now),
        )
        conn.execute(
            """
            INSERT INTO message_labels(msg_pk, account_id, gmail_label_id, observed_at)
            VALUES (?, ?, 'STARRED', ?)
            """,
            (msg, account, now),
        )

add(1, "secretbodyqx", True)
add(2, "hello there", False)
conn.commit()
conn.close()
PY
rules_out="$(
  cd "${REPO_ROOT}"
  python3 -m mailcrawl --json rules \
    --account vcl \
    --rules testdata/rules/rules-v0.json \
    --now 1700000000
)"
echo "${rules_out}" | tee "${PROOF_DIR}/rules.log"
printf '%s' "${rules_out}" | grep -q secretbodyqx && fail "rules output included message text"
RULES_JSON="${rules_out}" python3 <<'PY' || fail "rules counts drifted"
import json, os
doc = json.loads(os.environ["RULES_JSON"])
if doc.get("rules_version") != "rules-v4" or doc.get("threads") != 2 or doc.get("covered") != 2:
    raise SystemExit(doc)
if doc.get("by_state") != {"needs_review": 1, "reply": 1}:
    raise SystemExit(doc.get("by_state"))
if doc.get("by_rule") != {"0": 1, "5": 1}:
    raise SystemExit(doc.get("by_rule"))
PY
(
  cd "${REPO_ROOT}"
  python3 -m mailcrawl --json rules \
    --account vcl \
    --rules testdata/rules/rules-v0.json \
    --now 1700000000
) >/dev/null
DECISIONS="$(python3 - "${VCL_DB}" <<'PY'
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute("SELECT count(*) FROM decisions").fetchone()[0])
PY
)"
[[ "${DECISIONS}" == "2" ]] || fail "rules rerun added decisions"

log "check: stats and dry-run print counts"
stats_out="$(
  cd "${REPO_ROOT}"
  python3 -m mailcrawl --json stats --account vcl
)"
dry_out="$(
  cd "${REPO_ROOT}"
  python3 -m mailcrawl --json dry-run --account vcl
)"
echo "${stats_out}" | tee "${PROOF_DIR}/stats.log"
echo "${dry_out}" | tee "${PROOF_DIR}/dry-run.log"
printf '%s%s' "${stats_out}" "${dry_out}" | grep -q secretbodyqx && fail "counts included message text"
printf '%s%s' "${stats_out}" "${dry_out}" | grep -q '@' && fail "counts included an address"
STATS_JSON="${stats_out}" DRY_JSON="${dry_out}" python3 <<'PY' || fail "stats or dry-run counts drifted"
import json, os
stats = json.loads(os.environ["STATS_JSON"])
dry = json.loads(os.environ["DRY_JSON"])
if stats.get("messages") != 2 or stats.get("threads") != 2 or stats.get("senders") != 2:
    raise SystemExit(stats)
if stats.get("by_month") != {"2023-11": 2}:
    raise SystemExit(stats.get("by_month"))
if dry.get("by_state") != {"needs_review_rule0": 1, "reply": 1}:
    raise SystemExit(dry.get("by_state"))
if dry.get("by_rule") != {"0": 1, "5": 1}:
    raise SystemExit(dry.get("by_rule"))
if dry.get("by_label") != {"STARRED": 1}:
    raise SystemExit(dry.get("by_label"))
if dry.get("by_sender") != {"1": 1, "2": 1}:
    raise SystemExit(dry.get("by_sender"))
if dry.get("by_month") != {"2023-11": 2}:
    raise SystemExit(dry.get("by_month"))
PY
DECISIONS_AFTER="$(python3 - "${VCL_DB}" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
print(conn.execute("SELECT count(*) FROM decisions").fetchone()[0])
plans = conn.execute(
    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'plans'"
).fetchone()
if plans is not None:
    raise SystemExit("plans table exists")
PY
)"
[[ "${DECISIONS_AFTER}" == "2" ]] || fail "stats or dry-run changed decisions"

log "check: Takeout mbox imports, reimports, and reconciles with a gog export, keeping pseudo-labels"
MBOX_DATA="${HOME}/mbox-data"
mbox_cli() {
  (
    cd "${REPO_ROOT}"
    python3 -m mailcrawl --json --home "${MBOX_DATA}" "$@"
  )
}
mbox_cli init --account pessoal --email ada@example.com >/dev/null
mbox_first="$(mbox_cli import mbox testdata/fixtures/mbox/takeout.mbox --alias pessoal)"
mbox_again="$(mbox_cli import mbox testdata/fixtures/mbox/takeout.mbox --account personal)"
mbox_gog="$(mbox_cli import gog-export --alias personal --dir testdata/fixtures/mbox/gog-after)"
printf '%s\n%s\n%s\n' "${mbox_first}" "${mbox_again}" "${mbox_gog}" | tee "${PROOF_DIR}/mbox.log"
FIRST="${mbox_first}" AGAIN="${mbox_again}" GOG="${mbox_gog}" DB_PATH="${MBOX_DATA}/personal.db" python3 <<'PY' || fail "mbox import counts drifted"
import json, os, sqlite3
first = json.loads(os.environ["FIRST"])
again = json.loads(os.environ["AGAIN"])
gog = json.loads(os.environ["GOG"])
if (first["n_seen"], first["n_new"], first["n_refused"], first["n_error"]) != (6, 5, 1, 0):
    raise SystemExit(f"first {first}")
if (again["n_new"], again["n_dup"]) != (0, 5):
    raise SystemExit(f"again {again}")
if (gog["n_new"], gog["n_dup"], gog["n_reconciled"], gog["n_error"]) != (0, 1, 1, 0):
    raise SystemExit(f"gog {gog}")
conn = sqlite3.connect(os.environ["DB_PATH"])
try:
    if conn.execute("SELECT count(*) FROM messages").fetchone()[0] != 5:
        raise SystemExit("message rows")
    if conn.execute("SELECT count(*) FROM labels WHERE gmail_label_id LIKE 'mbox:%'").fetchone()[0]:
        raise SystemExit("provisional labels left")
    if conn.execute("SELECT count(*) FROM message_labels WHERE gmail_label_id LIKE 'pseudo:%'").fetchone()[0] != 4:
        raise SystemExit("pseudo-labels did not survive the gog import")
finally:
    conn.close()
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
        "body_text_cleaned",
        "native_gog_export",
        "attachment_parse",
        "rules_v0",
        "stats_dry_run",
        "mbox_import",
        "missing_required_key_unsupported_schema",
        "fixture_sha256_unchanged",
    ],
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(doc, handle, indent=2)
    handle.write("\n")
PY

log "phase 1 proof passed"
