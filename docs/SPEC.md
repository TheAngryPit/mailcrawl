# mailcrawl: spec v0

Status: phase 1 is in the tree. Schema v1, idempotent import, FTS search, and the JSON CLI. Repo: `TheAngryPit/mailcrawl`. License: MIT.

**Local-only.** mailcrawl archives mail that is already on disk. It does not open a network connection, does not read credentials, and does not write to Gmail. Unknown export shapes fail closed.

## 1. What it is
A local-first, read-only archive of your mail. Phase 1 is a Python prototype: a SQLite schema, an idempotent import of a gog export, FTS5, a JSON CLI, and fixture tests. This revision adds schema v1, an idempotent import of the v0 fixture contract, FTS search, a JSON CLI, and a reader for a gog backup plaintext export.

No servers, no cloud copy, no writes back to Gmail. The program never runs `gog`. No language model runs on the crawl path.

## 2. Principles
- **Read-only.** mailcrawl writes only its own config, cache, and archive. It never writes to Gmail.
- **Local-first.** Archives live under the user's home with private permissions (0700/0600). Phase 1 has no network listener. Search, stats, and show do not use the network.
- **No credentials in the tree.** OAuth tokens, age keys, and real exports never enter the repository or a log.
- **Honest outcomes.** Unknown or unverified export schemas fail closed (`unsupported_schema`) instead of guessing.
- **Privacy by default.** Bodies, subjects, and addresses from real mail never go to GitHub. The repository holds code, schema, and metadata. Tests use synthetic fixtures only (`example.com`, obvious fakes).
- **No language model on the crawl path.**
- **Neutral core.** No real mailbox, no real names, and no personal data in the repo.

## 3. Repo layout (scaffold, Python)
```
docs/SPEC.md
Makefile                         # make check
scripts/hygiene.sh               # tracked-file and secret gate
scripts/proof.sh                 # scaffold proof, temp HOME, no network
mailcrawl/contract.py            # v0 fixture contract
mailcrawl/gognative.py           # gog backup plaintext export
mailcrawl/eml.py                 # RFC822 parse for that export
mailcrawl/schema.py              # schema v1 DDL
mailcrawl/store.py               # open one account database and migrate it
mailcrawl/ingest.py              # idempotent gog-export import
mailcrawl/search.py              # FTS search
mailcrawl/cli.py                 # init, import, and search
tests/
testdata/fixtures/gog-export/    # synthetic; v0 fixture contract and native gog layout
testdata/catalogue/              # synthetic taxonomy stand-in
skills/mailcrawl/SKILL.md
.cursor/skills/verify-mailcrawl/
```
Phase 1 language is Python. A Go port is later, after the prototype settles. This revision does not add a Go module.

## 4. Phase 1 contract
Phase 1 is read-only Python and it does all of the following:

- Create SQLite schema v1 for one local archive. `mailcrawl/schema.py` migrates an empty database to `user_version` 1.
- Import a gog export idempotently on `(account_id, gmail_message_id)`. `mailcrawl import gog-export` does that for the fixture contract and for a gog backup plaintext export.
- Index text with FTS5 using the tokenizer `unicode61 remove_diacritics 2`. Import writes `msg_fts`. `mailcrawl search` reads it.
- Speak JSON on the CLI. `init`, `import`, and `search` accept `--json` before the command.
- Test against synthetic fixtures only.

`mailcrawl.contract.classify_export` returns `ok` for a `mailcrawl.gog-export.v0` document that has the four required keys from section 6 point 4. It returns `unsupported_schema` when the schema label differs or any of those keys is missing. The key names belong to the fixture contract. They are not a map of live gog field names.

`import gog-export` also reads a gog backup plaintext export when the directory has no `export.json`. The gogcli source writes `gmail/<account>/labels.json` and `gmail/<account>/messages/index.jsonl`. Each index record's `eml` value is a slash-separated path relative to the `gog backup export --out` directory. Pass that directory as `--dir`, or pass `gmail/<account>`. Paths that leave the export directory are rejected. A missing `.eml` file is `missing_file` and exits 1. A malformed record is `malformed_record` and exits 3. An unrecognized directory is `unsupported_schema` and exits 3. None of those errors include a message body or an address. A native gog import of a real thehive export was verified on the Mac at commit `32b936c`. The export had 25 messages. The first import counted 25 new, 0 duplicates, and 0 errors. The second import counted 0 new, 25 duplicates, and 0 errors. Two searches each returned 13 hits. Both searches returned the same `gmail_message_id` values. The database stayed at 25 messages. The synthetic fixture under `testdata/fixtures/gog-export/native/` is still the behavior this repository checks.

## 5. Phase 1 proof
A phase 1 change is done when, on a clean Linux runner with no network credentials and a temporary `HOME`, this exits 0:

```
make check && scripts/proof.sh
```

`scripts/proof.sh` uses a temp `HOME`, blocks proxy egress, and reads only the repository. It must show:

1. Required docs and the MIT `LICENSE` are present.
2. Synthetic fixtures under `testdata/` are present, use `example.com`, and are not gitignored.
3. Local archive paths, SQLite files, real-mail exports, models, and secret files are ignored, and none of those are tracked.
4. Python tests that exist pass.
5. `init --account pessoal` creates `personal.db` and does not create `index.db`, `reads.db`, or `pessoal.db`.
6. Import counts match `example_manifest.messages`.
7. A second import keeps the same ordered-row-dump hash.
8. `search reuniao` finds the fixture text `reunião`.
9. Source fixture hashes are unchanged after import and search.
10. A `mailcrawl.gog-export.v0` document that lacks any of the four required keys fails closed with `unsupported_schema`.
11. The synthetic native gog export imports, a second import counts every row as a duplicate, and `search reuniao` finds `reunião` in that export.

## 6. Closed decisions
These points are closed.

1. Schema v1 (`user_version=1`) contains only the tables ingest and FTS write. Those tables are `accounts`, `labels`, `senders`, `threads`, `messages`, `headers`, `message_labels`, `attachments`, `cursors`, `import_runs`, `ledger`, and `msg_fts`. Decisions, plans, embeddings, and clips arrive in a later migration when code writes them. v1 does not create empty tables for them.
2. The account alias is `personal`. `pessoal` is an optional CLI input alias that selects `personal`. The database file is `personal.db`.
3. `provenance_hash` is the SHA-256 of five lines. The lines are `gog`, the stable alias, `gmail_message_id`, `raw_sha256`, and the mailcrawl version. The hash does not use `account_id`. The version is the package semver plus a short git SHA when git can see one, for example `0.1.0+abc1234`.
4. The fixture contract stays `mailcrawl.gog-export.v0`. The four required keys are message id, thread id, label ids, and date. A document that lacks any of those four fails closed with `unsupported_schema`. Other listed fields are optional and stay NULL when the export omits them. Unknown keys go to one raw JSON field. Fixture key names belong to this contract. They are not gog field names. The gog plaintext export is a second layout, read from gogcli `gmailExportIndexEntry` and `gmailbackup.Label`. Index fields are `id`, `threadId`, `historyId`, `internalDate`, `labelIds`, `sizeEstimate`, `subject`, `from`, `to`, `cc`, `date`, and `eml`. `to` and `cc` are arrays of strings. `internalDate` is unix milliseconds and is stored as unix seconds. `labels.json` is an array. Each entry uses `id`, `name`, and `type` (`system` or `user`). Message ids, thread ids, and label names are stored as written. A record needs `id`, `threadId`, and either `internalDate` or `date`, plus an `eml` path. `labelIds` may be absent. Unknown index keys go to the same raw JSON field. `markdown` and `attachments` are known and are not a second body. Markdown-only records, which have no `eml`, are rejected.
5. `body_text` is the `text/plain` part. When the message has no plain part, `body_text` is the HTML part converted to text. The stored text includes quotations and signatures. Quote and signature stripping is phase 2. Phase 2 keeps this original text.
6. `msg_fts` indexes `subject`, `body_text`, `from_hdr`, and `attachment_text`. The tokenizer is `unicode61 remove_diacritics 2`. `attachment_text` stays empty, and `extract_status` stays `pending`, unless the MIME part is real text. A text part stores the decoded text with `extract_status` `ok`. Binary parts are not decoded as text. Addresses in the local index stay on the machine.
7. `init` creates one database per alias, named `<alias>.db`. It does not create `index.db` or `reads.db`.
8. Real label names and `taxonomy-account-maps.json` stay off this repository. Tests load `testdata/catalogue/taxonomy.synthetic.json`. A real catalogue is a local file passed with `--taxonomy` or the config key `taxonomy`.
9. `headers` stores an allow-list, in lower case. The names are `list-unsubscribe`, `list-unsubscribe-post`, `list-id`, `precedence`, `auto-submitted`, `x-mailer`, `feedback-id`, `message-id`, `in-reply-to`, `references`, `reply-to`, `return-path`, `authentication-results`, and `received-spf`. Other headers are dropped.
10. `snippet` and `gmail_history_id` are NULL when the export omits them. `subject_norm` trims the subject, removes a leading run of `Re:`, `Fwd:`, and `Fw:` without regard to case, and collapses whitespace. It does not fold accents.

## 7. Scope
**In this revision:** spec, README, security policy, contributing guide, `make check`, CI, synthetic fixtures, schema v1, `init`, idempotent `import gog-export` for the fixture contract and for a gog backup plaintext export, FTS search, and the phase 1 proof.

**Out of scope:** embeddings, label apply, Gmail writes, running `gog`, a language model on the crawl path, markdown-only gog exports, and the Go port. Section 8 lists later phases. Those phases are planned and out of this revision. Section 4 records a 25-message thehive smoke import. A full mailbox import is phase 2, not this revision.

## 8. Later phases
Status: PLANNED. This section is out of the current scope. Sections 4 to 7 describe the work this repository does now. The phases below are not requirements of this revision.

An inference stays marked as still to be decided. An open approval stays marked as still to be decided.

### Phase 2. Full thehive backup and export
The work is a full thehive backup and export, the parser and OCR, rules 0 to 8, `stats`, and dry-run counts. The dry-run writes nothing.

Acceptance is coverage of 100% of Gmail threads. The known 404 stays outside that coverage. Acceptance also includes a coverage report and a review of 100 sample decisions.

An error under 2% on that sample is an inference. It is still to be decided. The full thehive backup needs Pit's approval. The cache-deletion policy needs Pit's approval. Both approvals are still to be decided.

### Phase 3. Embeddings
The work is benchmarks B1 to B5, a choice of model and dimension, and clusters.

Acceptance is decision rule G.6. The text of that rule is outside this spec.

Model download needs approval. The license read needs approval. Image embedding of scans needs approval. Those approvals are still to be decided.

### Phase 4. First Gmail write
The work is a 100-message plan on thehive. The sequence is a dry-run, a before-state, the apply step, a readback, and the ledger.

Acceptance is 100% verified, and 0 unexpected label changes.

The write needs explicit approval from Pit before it runs. The approved write adds existing labels only, and it runs on thehive only. That approval is still to be decided.

### Phase 5. Other accounts by age cohorts
The work is the vcl account, then personal, by age cohorts. Cohorts run oldest first. The last 90 days go thread by thread. Section G of an earlier plan holds the rest of the cohort order. That section is outside this spec.

Acceptance is per-cohort verification, and drift is 0.

Each cohort needs approval. Archive rules need approval. Both are still to be decided.

### Phase 6. Daily sync
The work is history sync, jornalzinho from the database, HEY lanes, and a weekly review.

Acceptance is that the inbox-zero definition in section J of an earlier plan holds for 7 days. That definition is outside this spec.

Schedules and routines need approval. That approval is still to be decided.

### Phase 7. Optional local language model
The work is advisory classification of `needs_review`.

Acceptance is that the slice beats the rules baseline on an audit sample, and that it uses no tools.

Any cloud language model needs approval. The default is no. That choice is still to be decided.

### Not yet specified
The phase plan leaves these items unspecified. They are undecided.

- Read-only MCP.
- Privacy classes.
- Go port.
- Markdown-only gog exports.

Section 6 point 4 still rejects a record with no `eml` path.
