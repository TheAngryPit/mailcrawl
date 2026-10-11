# mailcrawl: spec v0

Status: schema v2 is in the tree. Idempotent import, cleaned `body_text`, FTS search, and the JSON CLI. Repo: `TheAngryPit/mailcrawl`. License: MIT.

**Local-only.** mailcrawl archives mail that is already on disk. It does not open a network connection, does not read credentials, and does not write to Gmail. Unknown export shapes fail closed.

## 1. What it is
A local-first, read-only archive of your mail. The prototype is Python: a SQLite schema, an idempotent import of a gog export, FTS5, a JSON CLI, and fixture tests. This revision adds schema v2, keeps the original body beside the cleaned body, and indexes only the cleaned text.

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
mailcrawl/extract.py             # attachment parser interface
mailcrawl/rules.py               # rules 0–8, no model
mailcrawl/schema.py              # schema v2 DDL
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

- Create SQLite schema v2 for one local archive. `mailcrawl/schema.py` migrates an empty database to `user_version` 2. A database at `user_version=1` gains `body_text_raw`, a cleaned `body_text`, a rebuilt `msg_fts`, and the empty tables `decisions`, `sender_decisions`, and `thread_mutes`.
- Import a gog export idempotently on `(account_id, gmail_message_id)`. `mailcrawl import gog-export` does that for the fixture contract and for a gog backup plaintext export.
- Index text with FTS5 using the tokenizer `unicode61 remove_diacritics 2`. Import writes `msg_fts` from the cleaned body. `mailcrawl search` reads it.
- Parse attachment bytes through `mailcrawl.extract`. `text/plain` and `text/html` are extracted. Other types stay behind that interface. No external binary is required.
- Apply rules 0 to 8 with `mailcrawl rules`. `rules_version` is `rules-v0`. The command prints counts only.
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
12. For the v0 fixture message that contains a `>` quote and a `-- ` signature, `body_text_raw` keeps both, `body_text` keeps only the sentence above them, and `search quoted` does not return that message. `search fake` does.
13. The v0 metadata attachment is `skipped` with reason `no_bytes`. The native text attachment is `ok`. The native octet-stream attachment is `unsupported` and is not searchable. OCR thresholds 25 and 60 run only when a backend is supplied.
14. `mailcrawl rules` on a synthetic account prints counts. A `STARRED` thread is `needs_review` under rule `0`. A plain thread is `reply` under rule `5`. A second run does not add a decision. The output has no message body.

## 6. Closed decisions
These points are closed.

1. Schema v1 (`user_version=1`) contained the ingest and FTS tables `accounts`, `labels`, `senders`, `threads`, `messages`, `headers`, `message_labels`, `attachments`, `cursors`, `import_runs`, `ledger`, and `msg_fts`. A database at `user_version=1` migrates to schema v2. Schema v2 adds `body_text_raw` and the empty tables `decisions`, `sender_decisions`, and `thread_mutes`. It does not create `plans`, `proposals`, embeddings, or clips.
2. The account alias is `personal`. `pessoal` is an optional CLI input alias that selects `personal`. The database file is `personal.db`.
3. `provenance_hash` is the SHA-256 of five lines. The lines are `gog`, the stable alias, `gmail_message_id`, `raw_sha256`, and the mailcrawl version. The hash does not use `account_id`. The version is the package semver plus a short git SHA when git can see one, for example `0.1.0+abc1234`.
4. The fixture contract stays `mailcrawl.gog-export.v0`. The four required keys are message id, thread id, label ids, and date. A document that lacks any of those four fails closed with `unsupported_schema`. Other listed fields are optional and stay NULL when the export omits them. Unknown keys go to one raw JSON field. Fixture key names belong to this contract. They are not gog field names. The gog plaintext export is a second layout, read from gogcli `gmailExportIndexEntry` and `gmailbackup.Label`. Index fields are `id`, `threadId`, `historyId`, `internalDate`, `labelIds`, `sizeEstimate`, `subject`, `from`, `to`, `cc`, `date`, and `eml`. `to` and `cc` are arrays of strings. `internalDate` is unix milliseconds and is stored as unix seconds. `labels.json` is an array. Each entry uses `id`, `name`, and `type` (`system` or `user`). Message ids, thread ids, and label names are stored as written. A record needs `id`, `threadId`, and either `internalDate` or `date`, plus an `eml` path. `labelIds` may be absent. Unknown index keys go to the same raw JSON field. `markdown` and `attachments` are known and are not a second body. Markdown-only records, which have no `eml`, are rejected.
5. `body_text_raw` is the phase 1 text, untouched. That text is the `text/plain` part, or the HTML part converted to text when the message has no plain part. `body_text` is that text after cleaning. Cleaning removes a line that starts with `>` once leading spaces are ignored. It also cuts from the first clear marker through the end of the text. The markers are a line `On … wrote:`, a line `Em … escreveu:`, a line `-----Original Message-----`, an Outlook header block that starts at `From:` or `De:` when `Sent:` or `Enviada:` appears within the next eight lines, and a signature that starts at a line that is exactly `-- ` (two dashes and a space). A line that is `--` with no space stays. Patterns are Portuguese and English only. When unsure, do not cut. The `.eml` file at `raw_path` stays the source of truth. A v2 migration copies the old `body_text` into `body_text_raw` and then cleans `body_text`.
6. `msg_fts` indexes `subject`, the cleaned `body_text`, `from_hdr`, and `attachment_text`. It does not index `body_text_raw`. The tokenizer is `unicode61 remove_diacritics 2`. A `text/plain` or `text/html` part with bytes stores the decoded text, `extract_status` `ok`, and `file_sha256`. A part with no bytes is `skipped` with reason `no_bytes` and a NULL hash. PDF, image OCR, Office, and `ics` are `skipped` when their backend is absent. Any other type is `unsupported`. Skipped and unsupported parts are not indexed as text. Addresses in the local index stay on the machine.
7. `init` creates one database per alias, named `<alias>.db`. It does not create `index.db` or `reads.db`.
8. Real label names and `taxonomy-account-maps.json` stay off this repository. Tests load `testdata/catalogue/taxonomy.synthetic.json`. A real catalogue is a local file passed with `--taxonomy` or the config key `taxonomy`.
9. `headers` stores an allow-list, in lower case. The names are `list-unsubscribe`, `list-unsubscribe-post`, `list-id`, `precedence`, `auto-submitted`, `x-mailer`, `feedback-id`, `message-id`, `in-reply-to`, `references`, `reply-to`, `return-path`, `authentication-results`, and `received-spf`. Other headers are dropped.
10. `snippet` and `gmail_history_id` are NULL when the export omits them. `subject_norm` trims the subject, removes a leading run of `Re:`, `Fwd:`, and `Fw:` without regard to case, and collapses whitespace. It does not fold accents.
11. Attachment parsing handles `text/plain` and `text/html` only. PDF, OCR, Office, `ics`, `attachment_fields`, and image embeddings stay behind an interface with no required implementation. An unsupported type is `unsupported`. A missing backend is `skipped`. A row with no `file_sha256` is `skipped` with reason `no_bytes`. OCR thresholds are v0 constants: fewer than 25 characters per page tries OCR, and `ocr_conf` below 60 is `low_confidence`. Those thresholds are exercised only when a backend is present. This revision adds that interface in `mailcrawl/extract.py`. The default backend does not invoke `pdftotext` or `tesseract`. Image embeddings and `attachment_fields` are not created.
12. Rules match the keys and axes of the catalogue loaded from a local file. The repository ships a synthetic catalogue with those axes and fictional leaves. Real label names never appear in code or tests. They stay in a local file outside Git.
13. Guards run first. A guard can only force `needs_review` or block `noise` and `resolved`. Later rules choose the other states. `STARRED` and `IMPORTANT` are Gmail system label ids.
14. These definitions are v0, tunable, and marked v0 in code. A human participant is a sender whose origin label is human. When that label is missing, the sender has no `List-Unsubscribe`, no `Precedence` of `bulk` or `list`, and no `Auto-Submitted` other than `no`. The label beats the header. Unrecognized Account & Security means a sender with no `sender_decisions` row. The read-list is `sender_decisions.route = 'read'`. There is no extra domain file. `read_later_expiry_days` is 30, and expiry is tested on fixtures. Open questions, concrete tasks, and closing language are short clue lists in Portuguese and English. They live in a configuration file, not in the engine. The minimum question clues are a `?` at the end of a sentence outside a quote, or a start of `can you`, `could you`, `podes`, `consegues`, or `pode`. The minimum task clues are `please`, `por favor`, `preciso que`, `need you to`, and `can you send`. The minimum closing clues are `thanks`, `obrigado`, `cumprimentos`, `best regards`, `perfect`, and `combinado`. That list is not final. The client plus-three-business-days path is unimplemented. Everyone uses plus seven days. `sender_decisions.client_sla_days` exists and is NULL by default. `sender_decisions.route` may be `imbox`, `feed`, `paper_trail`, or `read`. Priority comes from `sender_decisions.priority`. When that row is missing, priority is NULL.
15. Coverage is the fraction of threads with a current decision. `needs_review` counts as covered. The report is counts by `next_state` and by `rule_id`. A sample of 100 and a 2% threshold are not a fixture gate.
16. Dry-run prints counts only: state, label, sender, month, and rule. It does not write a plan file and it does not touch Gmail. Plans are phase 4.
17. `rules_version` is `rules-v0`. A change to a rule, a constant, or a list increments it. `evidence` stores ids and rule or label names only, never email text.
18. No language model runs on the crawl. The first matching rule wins. There is no Gmail write, no network, and no `gog`.

## 7. Scope
**In this revision:** spec, README, security policy, contributing guide, `make check`, CI, synthetic fixtures, schema v2, body cleaning, the attachment parser interface, rules 0 to 8, `init`, idempotent `import gog-export` for the fixture contract and for a gog backup plaintext export, FTS search over the cleaned body, and the phase 1 proof.

**Out of scope:** `stats` and dry-run counts are closed in section 6 and land in a later phase 2 revision. Embeddings, label apply, Gmail writes, running `gog`, a language model on the crawl path, a full thehive backup, cache deletion, markdown-only gog exports, and the Go port stay out. Section 4 records a 25-message thehive smoke import. A full mailbox import is not this revision.

## 8. Later phases
Body cleaning, schema v2, the attachment interface, and rules 0 to 8 are in this revision. Sections 4 to 7 describe that work. `stats` and dry-run counts stay specified in section 6 and are not implemented here.

An inference stays marked as still to be decided. An open approval stays marked as still to be decided.

### Phase 2. Cleaned text, parser interface, rules, and counts
Body cleaning, the attachment interface, and rules 0 to 8 are done. `rules_version` is `rules-v0`. There is no model. The remaining work is `stats` and dry-run counts. Dry-run prints counts only. It writes no plan file and it does not touch Gmail.

A full thehive backup is not part of phase 2. Cache deletion is not part of phase 2. A sample of 100 and an error under 2% are not a fixture gate. Coverage is the counts in section 6 point 15.

### Phase 3. Embeddings
The local baseline is EmbeddingGemma 2, stored with sqlite-vec at 256 or 768 dimensions. The choice of model and dimension is still open. `mailcrawl embed` is a separate step outside the crawl path.

The fallback named in the plan is EmbeddingGemma 1. That fallback is an inference, and it is not benchmarked. It is still to be decided.

The benchmarks are B1 retrieval, B2 label prediction, B3 scans, B4 cost, and B5 sanity. Clusters are part of this phase. Portuguese pt-PT quality is unverified. Speed on Pit's Mac is unverified. The full license text is unverified. Those three wait on benchmarks B1 to B5.

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
The work is a small local decision model. It is advisory only. It may suggest triage decisions and reply drafts for messages that deterministic rules leave in `needs_review`. It has no tools. It does not write to Gmail. Pit approves before any write.

The concrete model is undefined. Pit mentioned a "laya decision model". That model is not identified yet.

Acceptance is that the slice beats the rules baseline on an audit sample, and that it uses no tools.

Any cloud language model needs approval. The default is no. That choice is still to be decided.

### Not yet specified
The phase plan leaves these items unspecified. They are undecided.

- Read-only MCP.
- Privacy classes.
- Go port.
- Markdown-only gog exports.

Section 6 point 4 still rejects a record with no `eml` path.
