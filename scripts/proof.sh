#!/usr/bin/env bash
# Phase 1 proof. Temp HOME, no credentials, no network.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROOF_DIR="${REPO_ROOT}/proof"
ORIG_HOME="${HOME}"
# HOME isolation moves the user site. An optional extra already on this interpreter stays visible.
VEC_SITE=""
if VEC_SITE="$(python3 -c 'import sqlite_vec, pathlib; print(pathlib.Path(sqlite_vec.__file__).resolve().parent.parent)' 2>/dev/null)"; then
  case ":${PYTHONPATH:-}:" in
    *":${VEC_SITE}:"*) ;;
    *) export PYTHONPATH="${VEC_SITE}${PYTHONPATH:+:${PYTHONPATH}}" ;;
  esac
else
  VEC_SITE=""
fi
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
if [[ -n "${VEC_SITE}" ]]; then
  python3 -c "import sqlite_vec" || fail "sqlite-vec vanished after HOME isolation"
fi

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
    if version != 7:
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

log "check: phase 2 keyword path"
PHASE2="${REPO_ROOT}/testdata/fixtures/phase2/keyword.mbox"
PHASE2_DATA="${PROOF_HOME}/phase2-data"
(
  cd "${REPO_ROOT}"
  python3 -m mailcrawl --home "${PHASE2_DATA}" --json init --account vcl --email ada@example.com
  python3 -m mailcrawl --home "${PHASE2_DATA}" --json import mbox "${PHASE2}" --account vcl
) | tee "${PROOF_DIR}/phase2-import.log"
python3 - "${PHASE2_DATA}/vcl.db" <<'PY' | tee "${PROOF_DIR}/phase2-keyword.log"
import sqlite3, sys
path = sys.argv[1]
conn = sqlite3.connect(path)
conn.row_factory = sqlite3.Row
try:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != 7:
        raise SystemExit(f"user_version {version}")
    flags = {
        row["subject"]: row["image_only"]
        for row in conn.execute(
            """
            SELECT messages.subject, threads.image_only
            FROM messages JOIN threads ON threads.thread_pk = messages.thread_pk
            """
        )
    }
    if flags.get("Reunião da fatura") != 1 or flags.get("Só imagem") != 1:
        raise SystemExit(f"image_only {flags}")
    if flags.get("Texto longo") != 0:
        raise SystemExit(f"long thread flagged {flags}")
    if len(flags) != 3:
        raise SystemExit(f"image_only {flags}")
    text = conn.execute(
        "SELECT extracted_text FROM attachments WHERE mime_type = 'application/pdf'"
    ).fetchone()[0]
    if "fatura FT99881" not in (text or ""):
        raise SystemExit(f"pdftotext {text!r}")
    print("image_only", flags)
    print("pdftotext", text.strip())
finally:
    conn.close()
PY
for query in reuniao reunião; do
  out="$(
    cd "${REPO_ROOT}"
    python3 -m mailcrawl --home "${PHASE2_DATA}" --json search "${query}" --account vcl
  )"
  echo "${out}" | tee "${PROOF_DIR}/phase2-fts-${query}.log"
  printf '%s' "${out}" | grep -q '"source": "fts"' || fail "fts miss ${query}"
done
frag="$(
  cd "${REPO_ROOT}"
  python3 -m mailcrawl --home "${PHASE2_DATA}" --json search 99881 --account vcl
)"
echo "${frag}" | tee "${PROOF_DIR}/phase2-trigram.log"
printf '%s' "${frag}" | grep -q '"source": "trigram"' || fail "trigram miss"
PHASE2_DATA="${PHASE2_DATA}" REPO_ROOT="${REPO_ROOT}" python3 - <<'PY' | tee "${PROOF_DIR}/phase2-quoted.log"
import json
import os
import subprocess
import sys

home = os.environ["PHASE2_DATA"]
repo = os.environ["REPO_ROOT"]
invoice = "188932a1fae40001"
prose = "188932a1fae40002"


def search(query: str) -> dict:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mailcrawl",
            "--home",
            home,
            "--json",
            "search",
            "--account",
            "vcl",
            "--",
            query,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"{query!r} exit {proc.returncode} {proc.stdout} {proc.stderr}")
    return json.loads(proc.stdout)


def ids(doc: dict) -> list[str]:
    return [hit["gmail_message_id"] for hit in doc["hits"]]


expect_empty = ['"', "-x", "AND", "*", "body_text:linha", "fat*", "NEAR(linha sintetica)"]
for query in expect_empty:
    found = ids(search(query))
    if found:
        raise SystemExit(f"{query!r} hit {found}")
    print("literal", query, "[]")
if ids(search("-linha")) != [prose]:
    raise SystemExit("minus token missed the prose")
if ids(search("linha")) != [prose]:
    raise SystemExit("linha missed the prose")
if ids(search("fatura")) != [invoice]:
    raise SystemExit("fatura missed the invoice")
phrase = search('"fatura curta"')
if ids(phrase) != [invoice] or phrase["hits"][0]["source"] != "fts":
    raise SystemExit(f"phrase {phrase}")
mixed = search('reunião "fatura')
if ids(mixed) != [invoice] or mixed["hits"][0]["source"] != "fts":
    raise SystemExit(f"mixed {mixed}")
short = search("da")
if ids(short) != [invoice] or short["hits"][0]["source"] != "fts":
    raise SystemExit(f"short {short}")
print("quoted operators ok")
PY
(
  cd "${REPO_ROOT}"
  python3 - <<'PY'
import sqlite3
from mailcrawl.schema import FTS_SQL, USER_VERSION
from mailcrawl.store import migrate

conn = sqlite3.connect(":memory:")
conn.executescript(
    """
    CREATE TABLE threads (
      thread_pk INTEGER PRIMARY KEY,
      account_id INTEGER NOT NULL,
      gmail_thread_id TEXT NOT NULL
    );
    CREATE TABLE messages (
      msg_pk INTEGER PRIMARY KEY,
      thread_pk INTEGER,
      subject TEXT,
      body_text TEXT,
      from_hdr TEXT
    );
    CREATE TABLE attachments (
      att_pk INTEGER PRIMARY KEY,
      msg_pk INTEGER,
      mime_type TEXT,
      size_bytes INTEGER,
      is_inline INTEGER NOT NULL DEFAULT 0,
      extracted_text TEXT
    );
    """
    + FTS_SQL
)
conn.execute("PRAGMA user_version = 4")
conn.execute("INSERT INTO threads(thread_pk, account_id, gmail_thread_id) VALUES (1, 1, 't')")
conn.execute(
    "INSERT INTO messages(msg_pk, thread_pk, subject, body_text, from_hdr) VALUES (1, 1, 'Reunião', 'A reunião', 'ada@example.com')"
)
conn.execute(
    "INSERT INTO attachments(msg_pk, mime_type, size_bytes, is_inline, extracted_text) VALUES (1, 'application/pdf', 80, 0, 'fatura FT99881')"
)
migrate(conn)
version = conn.execute("PRAGMA user_version").fetchone()[0]
flag = conn.execute("SELECT image_only FROM threads").fetchone()[0]
trigram = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'msg_trigram'").fetchone()[0]
names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
if version != USER_VERSION or flag != 1 or "trigram" not in trigram:
    raise SystemExit(f"migration {version} {flag} {trigram}")
if "vec_classification" in names or "vec_search" in names or "vec_image" in names or "embeddings" in names:
    raise SystemExit(f"vec tables {sorted(names)}")
print(f"user_version {version}")
print("image_only", flag)
print(trigram)
print("vec absent")
PY
) | tee "${PROOF_DIR}/phase2-migration.log"

log "check: phase 3 vectors stay off until config turns them on"
ARTIFACT_DIR="${ARTIFACT_DIR:-$PROOF_DIR}"
mkdir -p "${ARTIFACT_DIR}"

publish_log() {
  local name="$1"
  if [[ "${ARTIFACT_DIR}" == "${PROOF_DIR}" ]]; then
    return 0
  fi
  cp "${PROOF_DIR}/${name}" "${ARTIFACT_DIR}/${name}"
}
PHASE3_DATA="${PROOF_HOME}/phase3-data"
PHASE3_CFG="${PROOF_HOME}/phase3-config.toml"
PHASE3_MBOX="${PROOF_HOME}/phase3.mbox"
cat >"${PHASE3_MBOX}" <<'MBOX'
From 1768000000000000201@example.com Thu Jan 15 12:00:00 +0000 2026
From: Bao Fixture <bao@example.com>
To: Ada Fixture <ada@example.com>
Subject: Alpha keyword
Date: Thu, 15 Jan 2026 12:00:00 +0000
Message-ID: <phase3-alpha@example.com>
X-GM-THRID: 1768000000000000201
X-GM-MSGID: 1768000000000000201
X-Gmail-Labels: Inbox
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

alpha keyword

From 1768000000000000202@example.com Thu Jan 15 12:00:00 +0000 2026
From: Bao Fixture <bao@example.com>
To: Ada Fixture <ada@example.com>
Subject: Vec only
Date: Thu, 15 Jan 2026 12:05:00 +0000
Message-ID: <phase3-vec@example.com>
X-GM-THRID: 1768000000000000202
X-GM-MSGID: 1768000000000000202
X-Gmail-Labels: Inbox
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

vec-only marker

From 1768000000000000203@example.com Thu Jan 15 12:00:00 +0000 2026
From: Bao Fixture <bao@example.com>
To: Ada Fixture <ada@example.com>
Subject: Neighbour reviewed
Date: Thu, 15 Jan 2026 12:10:00 +0000
Message-ID: <phase3-near@example.com>
X-GM-THRID: 1768000000000000203
X-GM-MSGID: 1768000000000000203
X-Gmail-Labels: Inbox
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

same neighbour text

From 1768000000000000204@example.com Thu Jan 15 12:00:00 +0000 2026
From: Bao Fixture <bao@example.com>
To: Ada Fixture <ada@example.com>
Subject: Neighbour target
Date: Thu, 15 Jan 2026 12:15:00 +0000
Message-ID: <phase3-target@example.com>
X-GM-THRID: 1768000000000000204
X-GM-MSGID: 1768000000000000204
X-Gmail-Labels: Inbox
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

same neighbour text

From 1768000000000000205@example.com Thu Jan 15 12:00:00 +0000 2026
From: Bao Fixture <bao@example.com>
To: Ada Fixture <ada@example.com>
Subject: Far note
Date: Thu, 15 Jan 2026 12:20:00 +0000
Message-ID: <phase3-far@example.com>
X-GM-THRID: 1768000000000000205
X-GM-MSGID: 1768000000000000205
X-Gmail-Labels: Inbox
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

completely different archive note

MBOX
(
  cd "${REPO_ROOT}"
  python3 -m mailcrawl --home "${PHASE3_DATA}" --config "${PHASE3_CFG}" --json init --account personal --email ada@example.com
  python3 -m mailcrawl --home "${PHASE3_DATA}" --config "${PHASE3_CFG}" --json import mbox "${PHASE3_MBOX}" --account personal
) | tee "${PROOF_DIR}/phase3-import.log"
PHASE3_DATA="${PHASE3_DATA}" PHASE3_CFG="${PHASE3_CFG}" REPO_ROOT="${REPO_ROOT}" python3 - <<'PY' | tee "${PROOF_DIR}/phase3.log"
import json
import os
import sqlite3
import subprocess
import sys

from mailcrawl.decisions import DecisionDraft, decide_once
from mailcrawl.schema import SCHEMA_SQL, USER_VERSION
from mailcrawl.store import connect, migrate

home = os.environ["PHASE3_DATA"]
config = os.environ["PHASE3_CFG"]
repo = os.environ["REPO_ROOT"]
db_path = os.path.join(home, "personal.db")


def run(args: list[str]) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, "-m", "mailcrawl", "--home", home, "--config", config, "--json", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if not proc.stdout:
        raise SystemExit(f"{args} exit {proc.returncode} {proc.stderr}")
    return proc.returncode, json.loads(proc.stdout)


code, embedded = run(["embed", "--account", "personal", "--embedder", "fake"])
if code != 0 or embedded.get("embedded") != 0:
    raise SystemExit(f"off embed {code} {embedded}")
conn = connect(db_path)
try:
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    target = conn.execute(
        """
        SELECT threads.gmail_thread_id
        FROM threads JOIN messages ON messages.thread_pk = threads.thread_pk
        WHERE messages.subject = 'Neighbour target'
        """
    ).fetchone()[0]
finally:
    conn.close()
if "vec_classification" in names or "vec_search" in names or "vec_image" in names:
    raise SystemExit(f"off vectors {sorted(names)}")
code, hits = run(["search", "--account", "personal", "--", "alpha"])
if code != 0 or [hit["source"] for hit in hits["hits"]] != ["fts"]:
    raise SystemExit(f"off search {hits}")
if hits["hits"][0]["subject"] != "Alpha keyword":
    raise SystemExit(f"off subject {hits}")
code, gemma = run(["embed", "--account", "personal", "--embedder", "gemma"])
if code != 0 or gemma.get("embedded") != 0:
    raise SystemExit(f"gemma off {code} {gemma}")
print("gemma off embedded 0")
code, knn = run(["knn", "--account", "personal", "--thread", target, "--embedder", "fake"])
if code != 0 or knn.get("origin") is not None:
    raise SystemExit(f"off knn {knn}")
print("off embedded 0")
print("off search fts")
print("off knn none")

text = open(config, encoding="utf-8").read()
if "[inference]" not in text:
    text = "[inference]\nembeddings = true\nsearch-embedding = true\n\n" + text
    open(config, "w", encoding="utf-8").write(text)

code, embedded = run(["embed", "--account", "personal", "--embedder", "scripted"])
if code != 0 or embedded.get("embedded") != 5:
    raise SystemExit(f"scripted embed {embedded}")
code, keyword = run(
    ["search", "--account", "personal", "--embedder", "scripted", "--k", "1", "--", "alpha"]
)
order = [(hit["subject"], hit["source"]) for hit in keyword["hits"]]
if order != [("Alpha keyword", "fts"), ("Vec only", "vec")]:
    raise SystemExit(f"keyword-first {order}")
code, fused = run(
    [
        "search",
        "--account",
        "personal",
        "--embedder",
        "scripted",
        "--semantic",
        "--vec-weight",
        "100",
        "--keyword-weight",
        "1",
        "--k",
        "1",
        "--",
        "alpha",
    ]
)
fused_order = [(hit["subject"], hit["source"]) for hit in fused["hits"]]
if fused_order != [("Vec only", "fused"), ("Alpha keyword", "fused")]:
    raise SystemExit(f"rrf {fused_order}")
print("fusion keyword", order)
print("fusion rrf", fused_order)

code, embedded = run(["embed", "--account", "personal", "--embedder", "fake"])
if code != 0 or embedded.get("embedded") != 5:
    raise SystemExit(f"fake embed {embedded}")
conn = connect(db_path)
try:
    threads = {
        row[1]: int(row[0])
        for row in conn.execute("SELECT thread_pk, subject FROM messages")
    }
    decide_once(
        conn,
        threads["Neighbour reviewed"],
        10,
        lambda: DecisionDraft(
            next_state="reference",
            lane="paper_trail",
            rule_id="3",
            rules_version="rules-v4",
            origin="human",
            source_detail="review",
        ),
    )
    decide_once(
        conn,
        threads["Far note"],
        10,
        lambda: DecisionDraft(
            next_state="noise",
            lane="feed",
            rule_id="else",
            rules_version="rules-v4",
            origin="human",
            source_detail="review",
        ),
    )
    target = conn.execute(
        "SELECT gmail_thread_id FROM threads WHERE thread_pk = ?",
        (threads["Neighbour target"],),
    ).fetchone()[0]
finally:
    conn.close()
code, knn = run(["knn", "--account", "personal", "--thread", target, "--embedder", "fake", "--now", "20"])
if code != 0 or knn.get("origin") != "knn" or knn.get("source_detail") != "fake-embedder":
    raise SystemExit(f"knn {knn}")
if knn.get("next_state") != "reference":
    raise SystemExit(f"knn state {knn}")
conn = sqlite3.connect(db_path)
try:
    evidence = conn.execute(
        """
        SELECT origin, source_detail, next_state, evidence
        FROM decisions
        WHERE thread_pk = ? AND superseded_by IS NULL
        """,
        (threads["Neighbour target"],),
    ).fetchone()
finally:
    conn.close()
if evidence[0] != "knn" or evidence[1] != "fake-embedder" or evidence[2] != "reference":
    raise SystemExit(f"stored knn {evidence}")
parsed = json.loads(evidence[3])
if parsed != [{"distance": 0.0, "neighbour_thread_pk": threads["Neighbour reviewed"]}]:
    raise SystemExit(f"neighbour {evidence[3]}")
print("knn", knn["origin"], knn["source_detail"], knn["next_state"])

fresh = sqlite3.connect(":memory:")
fresh.executescript(SCHEMA_SQL)
fresh.execute("PRAGMA user_version = 5")
migrate(fresh)
version = fresh.execute("PRAGMA user_version").fetchone()[0]
names = {row[0] for row in fresh.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
if version != USER_VERSION or version != 7:
    raise SystemExit(f"v5 migration {version}")
if "vec_classification" in names or "vec_search" in names or "vec_image" in names or "embeddings" in names:
    raise SystemExit(f"v5 tables {sorted(names)}")
print("migration", version, "vec absent")
PY
publish_log phase3.log

log "check: phase 3b image_only threads get a fake kNN neighbour"
PHASE3B_DATA="${PROOF_HOME}/phase3b-data"
PHASE3B_CFG="${PROOF_HOME}/phase3b-config.toml"
PHASE3B_MBOX="${PROOF_HOME}/phase3b.mbox"
python3 - "${PHASE3B_MBOX}" <<'PY'
import sys
encoded = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVQI12P4z8AAAAMBAQAY3Y20AAAAAElFTkSuQmCC"
text = f"""From 1768000000000000401@example.com Thu Jan 15 12:00:00 +0000 2026
From: Bao Fixture <bao@example.com>
To: Ada Fixture <ada@example.com>
Subject: Text note
Date: Thu, 15 Jan 2026 12:00:00 +0000
Message-ID: <phase3b-text@example.com>
X-GM-THRID: 1768000000000000401
X-GM-MSGID: 1768000000000000401
X-Gmail-Labels: Inbox
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

alpha keyword text path stays on the classification prefix

From 1768000000000000402@example.com Thu Jan 15 12:00:00 +0000 2026
From: Bao Fixture <bao@example.com>
To: Ada Fixture <ada@example.com>
Subject: Image reviewed
Date: Thu, 15 Jan 2026 12:05:00 +0000
Message-ID: <phase3b-reviewed@example.com>
X-GM-THRID: 1768000000000000402
X-GM-MSGID: 1768000000000000402
X-Gmail-Labels: Inbox
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="imgbound"

--imgbound
Content-Type: text/plain; charset="utf-8"

scan

--imgbound
Content-Type: image/png; name="scan.png"
Content-Disposition: attachment; filename="scan.png"
Content-Transfer-Encoding: base64

{encoded}
--imgbound--

From 1768000000000000403@example.com Thu Jan 15 12:00:00 +0000 2026
From: Bao Fixture <bao@example.com>
To: Ada Fixture <ada@example.com>
Subject: Image target
Date: Thu, 15 Jan 2026 12:10:00 +0000
Message-ID: <phase3b-target@example.com>
X-GM-THRID: 1768000000000000403
X-GM-MSGID: 1768000000000000403
X-Gmail-Labels: Inbox
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="imgbound2"

--imgbound2
Content-Type: text/plain; charset="utf-8"

scan

--imgbound2
Content-Type: image/png; name="scan.png"
Content-Disposition: attachment; filename="scan.png"
Content-Transfer-Encoding: base64

{encoded}
--imgbound2--
"""
open(sys.argv[1], "w", encoding="utf-8").write(text)
PY
(
  cd "${REPO_ROOT}"
  python3 -m mailcrawl --home "${PHASE3B_DATA}" --config "${PHASE3B_CFG}" --json init --account personal --email ada@example.com
  python3 -m mailcrawl --home "${PHASE3B_DATA}" --config "${PHASE3B_CFG}" --json import mbox "${PHASE3B_MBOX}" --account personal
) | tee "${PROOF_DIR}/phase3b-import.log"
PHASE3B_DATA="${PHASE3B_DATA}" PHASE3B_CFG="${PHASE3B_CFG}" REPO_ROOT="${REPO_ROOT}" python3 - <<'PY' | tee "${PROOF_DIR}/phase3b.log"
import os
import sqlite3
import subprocess
import sys

from mailcrawl.decisions import DecisionDraft, decide_once
from mailcrawl.store import connect
from mailcrawl.vectors import CLASSIFICATION_PREFIX, require_vec

home = os.environ["PHASE3B_DATA"]
config = os.environ["PHASE3B_CFG"]
repo = os.environ["REPO_ROOT"]
db_path = os.path.join(home, "personal.db")
text = open(config, encoding="utf-8").read()
if "[inference]" not in text:
    open(config, "w", encoding="utf-8").write("[inference]\nembeddings = true\n\n" + text)


def run(args):
    proc = subprocess.run(
        [sys.executable, "-m", "mailcrawl", "--home", home, "--config", config, "--json", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"{args} {proc.returncode} {proc.stdout} {proc.stderr}")
    import json
    return json.loads(proc.stdout)


conn = connect(db_path)
try:
    threads = {row[1]: int(row[0]) for row in conn.execute("SELECT thread_pk, subject FROM messages")}
    flags = {
        row[0]: int(row[1])
        for row in conn.execute(
            "SELECT messages.subject, threads.image_only FROM messages JOIN threads ON threads.thread_pk = messages.thread_pk"
        )
    }
    if flags["Text note"] != 0 or flags["Image reviewed"] != 1 or flags["Image target"] != 1:
        raise SystemExit(f"image_only {flags}")
    decide_once(
        conn,
        threads["Image reviewed"],
        10,
        lambda: DecisionDraft(
            next_state="reference",
            lane="paper_trail",
            rule_id="3",
            rules_version="rules-v4",
            origin="human",
            source_detail="review",
        ),
    )
    target = conn.execute(
        "SELECT gmail_thread_id FROM threads WHERE thread_pk = ?",
        (threads["Image target"],),
    ).fetchone()[0]
finally:
    conn.close()
embedded = run(["embed", "--account", "personal", "--embedder", "fake"])
if embedded.get("embedded") != 3:
    raise SystemExit(f"image embed {embedded}")
knn = run(["knn", "--account", "personal", "--thread", target, "--embedder", "fake", "--now", "20"])
if knn.get("origin") != "knn" or knn.get("source_detail") != "fake-embedder" or knn.get("next_state") != "reference":
    raise SystemExit(f"image knn {knn}")
conn = connect(db_path)
require_vec(conn)
try:
    text_prefix = conn.execute(
        "SELECT prefix FROM vec_classification WHERE thread_pk = ?",
        (threads["Text note"],),
    ).fetchone()[0]
    image_count = conn.execute("SELECT count(*) FROM vec_image").fetchone()[0]
finally:
    conn.close()
if text_prefix != CLASSIFICATION_PREFIX or image_count != 2:
    raise SystemExit(f"text prefix {text_prefix} images {image_count}")
print("image knn", knn["origin"], knn["source_detail"], knn["next_state"])
print("text prefix kept")
PY
publish_log phase3b.log

log "check: phase 4 jev stays off until config turns it on"
PHASE4_DATA="${PROOF_HOME}/phase4-data"
PHASE4_CFG="${PROOF_HOME}/phase4-config.toml"
PHASE4_MBOX="${PROOF_HOME}/phase4.mbox"
cat >"${PHASE4_MBOX}" <<'MBOX'
From 1768000000000000301@example.com Thu Jan 15 12:00:00 +0000 2026
From: Bao Fixture <bao@example.com>
To: Ada Fixture <ada@example.com>
Subject: Reply note
Date: Thu, 15 Jan 2026 12:00:00 +0000
Message-ID: <phase4-reply@example.com>
X-GM-THRID: 1768000000000000301
X-GM-MSGID: 1768000000000000301
X-Gmail-Labels: Inbox
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

please reply shared token shared token

From 1768000000000000302@example.com Thu Jan 15 12:00:00 +0000 2026
From: Bao Fixture <bao@example.com>
To: Ada Fixture <ada@example.com>
Subject: Late note
Date: Thu, 15 Jan 2026 12:05:00 +0000
Message-ID: <phase4-late@example.com>
X-GM-THRID: 1768000000000000302
X-GM-MSGID: 1768000000000000302
X-Gmail-Labels: Inbox
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

shared token answers-query

From 1768000000000000303@example.com Thu Jan 15 12:00:00 +0000 2026
From: Bao Fixture <bao@example.com>
To: Ada Fixture <ada@example.com>
Subject: Grey note
Date: Thu, 15 Jan 2026 12:10:00 +0000
Message-ID: <phase4-grey@example.com>
X-GM-THRID: 1768000000000000303
X-GM-MSGID: 1768000000000000303
X-Gmail-Labels: Inbox
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

verify your password shared token

MBOX
(
  cd "${REPO_ROOT}"
  python3 -m mailcrawl --home "${PHASE4_DATA}" --config "${PHASE4_CFG}" --json init --account personal --email ada@example.com
  python3 -m mailcrawl --home "${PHASE4_DATA}" --config "${PHASE4_CFG}" --json import mbox "${PHASE4_MBOX}" --account personal
) | tee "${PROOF_DIR}/phase4-import.log"
PHASE4_DATA="${PHASE4_DATA}" PHASE4_CFG="${PHASE4_CFG}" REPO_ROOT="${REPO_ROOT}" python3 - <<'PY' | tee "${PROOF_DIR}/phase4.log"
import json
import os
import sqlite3
import subprocess
import sys

home = os.environ["PHASE4_DATA"]
config = os.environ["PHASE4_CFG"]
root = os.environ["REPO_ROOT"]
db_path = os.path.join(home, "personal.db")
conn = sqlite3.connect(db_path)
try:
    threads = {
        row[0]: row[1]
        for row in conn.execute(
            """
            SELECT messages.subject, threads.gmail_thread_id
            FROM messages JOIN threads ON threads.thread_pk = messages.thread_pk
            """
        )
    }
finally:
    conn.close()
reply_id = threads["Reply note"]
grey_id = threads["Grey note"]

def run(args):
    proc = subprocess.run(
        [sys.executable, "-m", "mailcrawl", "--home", home, "--config", config, "--json", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"{args} {proc.returncode} {proc.stdout} {proc.stderr}")
    return json.loads(proc.stdout)

off = run(["jev", "--account", "personal", "--thread", reply_id, "--client", "fake", "--now", "10"])
if off.get("origin") is not None or off.get("next_state") != "needs_review" or off.get("decision_id") is not None:
    raise SystemExit(f"off jev {off}")
conn = sqlite3.connect(db_path)
try:
    count = conn.execute("SELECT count(*) FROM decisions").fetchone()[0]
finally:
    conn.close()
if count != 0:
    raise SystemExit(f"off decisions {count}")
print("off jev needs_review")
plain = run(["search", "--account", "personal", "--", "shared"])
if not plain["hits"] or plain["hits"][0]["subject"] != "Reply note" or any(hit["source"] == "jev" for hit in plain["hits"]):
    raise SystemExit(f"off search {plain}")
print("off search", plain["hits"][0]["subject"], plain["hits"][0]["source"])
with open(config, "a", encoding="utf-8") as handle:
    handle.write("\n[inference]\njev-battery = true\nsearch-reranker = true\n")
first = run(["jev", "--account", "personal", "--thread", reply_id, "--client", "fake", "--now", "10"])
second = run(["jev", "--account", "personal", "--thread", reply_id, "--client", "fake", "--now", "11"])
if first.get("origin") != "model" or first.get("source_detail") != "jev-1.13.0" or first.get("next_state") != "reply":
    raise SystemExit(f"stored jev {first}")
if second.get("decision_id") != first.get("decision_id"):
    raise SystemExit(f"second jev {second}")
conn = sqlite3.connect(db_path)
try:
    stored = conn.execute(
        "SELECT count(*) FROM decisions WHERE thread_pk = (SELECT thread_pk FROM threads WHERE gmail_thread_id = ?)",
        (reply_id,),
    ).fetchone()[0]
finally:
    conn.close()
if stored != 1:
    raise SystemExit(f"decision rows {stored}")
print("jev", first["origin"], first["source_detail"], first["next_state"], first["decision_id"])
grey = run(["jev", "--account", "personal", "--thread", grey_id, "--client", "fake", "--now", "10"])
grey_again = run(["jev", "--account", "personal", "--thread", grey_id, "--client", "fake", "--now", "11"])
if grey.get("origin") != "model" or grey.get("source_detail") != "jev-1.13.0" or grey.get("next_state") != "needs_review":
    raise SystemExit(f"grey {grey}")
if grey_again.get("decision_id") != grey.get("decision_id"):
    raise SystemExit(f"grey again {grey_again}")
print("grey", grey["next_state"], grey["source_detail"])
ranked = run(["search", "--account", "personal", "--jev-client", "fake", "--", "shared"])
if len(ranked["hits"]) != len(plain["hits"]):
    raise SystemExit(f"dropped {ranked}")
if ranked["hits"][0]["subject"] != "Late note" or ranked["hits"][0]["source"] != "jev":
    raise SystemExit(f"rerank {ranked}")
print("rerank", [(hit["subject"], hit["source"]) for hit in ranked["hits"]])
batch = run(["jev", "--account", "personal", "--client", "fake", "--now", "12"])
if (
    batch.get("eligible") != 1
    or batch.get("decided") != 1
    or batch.get("review") != 0
    or batch.get("account") != "personal"
    or batch.get("ok") is not True
):
    raise SystemExit(f"batch {batch}")
conn = sqlite3.connect(db_path)
try:
    late = conn.execute(
        """
        SELECT decisions.next_state, decisions.source_detail
        FROM decisions JOIN threads ON threads.thread_pk = decisions.thread_pk
        JOIN messages ON messages.thread_pk = threads.thread_pk
        WHERE messages.subject = 'Late note' AND decisions.superseded_by IS NULL
        """
    ).fetchone()
    reply_still = conn.execute(
        "SELECT decision_id FROM decisions WHERE decision_id = ?",
        (first["decision_id"],),
    ).fetchone()
finally:
    conn.close()
if late != ("reference", "jev-1.13.0") or reply_still is None:
    raise SystemExit(f"batch stored {late} {reply_still}")
again = run(["jev", "--account", "personal", "--client", "fake", "--now", "13"])
if again.get("eligible") != 0 or again.get("decided") != 0 or again.get("review") != 0:
    raise SystemExit(f"batch again {again}")
print("batch", batch["eligible"], batch["decided"], again["eligible"])
PY
publish_log phase4.log

log "check: bench compares two fake embedders on a read-only copy"
BENCH_DATA="${PROOF_HOME}/bench-data"
BENCH_CFG="${PROOF_HOME}/bench-config.toml"
BENCH_MBOX="${PROOF_HOME}/bench.mbox"
python3 - "${BENCH_MBOX}" <<'PY'
import base64
import sys
from pathlib import Path

encoded = base64.b64encode(base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVQI12P4z8AAAAMBAQAY3Y20AAAAAElFTkSuQmCC"
)).decode("ascii")
Path(sys.argv[1]).write_text(
    f"""From 1768000000000000701@example.com Thu Jan 15 12:00:00 +0000 2026
From: Bao Fixture <bao@example.com>
To: Ada Fixture <ada@example.com>
Subject: Alpha note
Date: Thu, 15 Jan 2026 12:00:00 +0000
Message-ID: <proof-bench-alpha@example.com>
X-GM-THRID: 1768000000000000701
X-GM-MSGID: 1768000000000000701
X-Gmail-Labels: Inbox
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

alpha bench

From 1768000000000000702@example.com Thu Jan 15 12:05:00 +0000 2026
From: Bao Fixture <bao@example.com>
To: Ada Fixture <ada@example.com>
Subject: Beta note
Date: Thu, 15 Jan 2026 12:05:00 +0000
Message-ID: <proof-bench-beta@example.com>
X-GM-THRID: 1768000000000000702
X-GM-MSGID: 1768000000000000702
X-Gmail-Labels: Inbox
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

beta bench

From 1768000000000000703@example.com Thu Jan 15 12:10:00 +0000 2026
From: Bao Fixture <bao@example.com>
To: Ada Fixture <ada@example.com>
Subject: Scan note
Date: Thu, 15 Jan 2026 12:10:00 +0000
Message-ID: <proof-bench-scan@example.com>
X-GM-THRID: 1768000000000000703
X-GM-MSGID: 1768000000000000703
X-Gmail-Labels: Inbox
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="proofbench"

--proofbench
Content-Type: text/plain; charset="utf-8"

scan bench

--proofbench
Content-Type: image/png; name="scan.png"
Content-Disposition: attachment; filename="scan.png"
Content-Transfer-Encoding: base64

{encoded}
--proofbench--

""",
    encoding="utf-8",
)
PY
(
  cd "${REPO_ROOT}"
  python3 -m mailcrawl --home "${BENCH_DATA}" --config "${BENCH_CFG}" --json init --account personal --email ada@example.com
  python3 -m mailcrawl --home "${BENCH_DATA}" --config "${BENCH_CFG}" --json import mbox "${BENCH_MBOX}" --account personal
)
python3 - "${BENCH_DATA}/personal.db" <<'PY'
import sys
from pathlib import Path

from mailcrawl.decisions import DecisionDraft, decide_once
from mailcrawl.store import connect

conn = connect(Path(sys.argv[1]))
try:
    rows = conn.execute("SELECT thread_pk FROM threads ORDER BY thread_pk").fetchall()
    for thread_pk in (int(rows[0][0]), int(rows[1][0])):
        decide_once(
            conn,
            thread_pk,
            10,
            lambda: DecisionDraft(
                next_state="reference",
                lane="paper_trail",
                rule_id="3",
                rules_version="rules-v4",
                origin="human",
                source_detail="review",
            ),
        )
finally:
    conn.close()
PY
before="$(sha256sum "${BENCH_DATA}/personal.db" | awk '{print $1}')"
(
  cd "${REPO_ROOT}"
  python3 -m mailcrawl --home "${BENCH_DATA}" --json bench --account personal --embedders fake,fake --sample 4
) | tee "${PROOF_DIR}/bench.json"
python3 - "${PROOF_DIR}/bench.json" "${PROOF_DIR}/bench.md" "${before}" "${BENCH_DATA}/personal.db" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

report_path, markdown_path, before, source = sys.argv[1:]
report = json.loads(Path(report_path).read_text(encoding="utf-8"))
Path(markdown_path).write_text(report["markdown"], encoding="utf-8")
digest = hashlib.sha256(Path(source).read_bytes()).hexdigest()
pair = report["pairs"][0]
if report.get("ok") is not True or digest != before:
    raise SystemExit(f"bench source {digest} {before} {report.get('ok')}")
if abs(pair["cosine_min"] - 1.0) > 1e-6 or abs(pair["knn_top10_overlap"] - 1.0) > 1e-6:
    raise SystemExit(f"bench pair {pair}")
decision = report["decisions"][0]
if decision["accuracy"] != 1.0 or decision["compared"] != 2 or report["n_with_images"] != 1 or report["n_human"] != 2:
    raise SystemExit(f"bench decisions {report['decisions']} counts {report['n_human']} {report['n_with_images']}")
if report["embedders"][0]["peak_rss_mb"] <= 1 or "peak_rss_bytes" in report["embedders"][0]:
    raise SystemExit(f"bench rss {report['embedders'][0]}")
if "(n=2)" not in report["markdown"]:
    raise SystemExit(report["markdown"])
if "db-v2" in report["vector_db"]:
    raise SystemExit(report["vector_db"])
print("bench", pair["left"], pair["right"], report["decisions"][0]["accuracy"])
PY
publish_log bench.json
publish_log bench.md

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
        "phase3_off_by_default",
        "phase3_fake_knn",
        "phase3_fusion",
        "phase3_migration",
        "phase3b_image_knn",
        "phase4_off_by_default",
        "phase4_fake_decision",
        "phase4_grey_review",
        "phase4_rerank",
        "phase4_batch",
        "bench_fake_pair",
        "fixture_sha256_unchanged",
    ],
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(doc, handle, indent=2)
    handle.write("\n")
PY

log "phase 1 proof passed"
