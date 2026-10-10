# mailcrawl: spec v0

Status: schema v1 is in the tree. Import, FTS search, and the CLI are specified and are not in this revision. Repo: `TheAngryPit/mailcrawl`. License: MIT.

**Local-only.** mailcrawl archives mail that is already on disk. It does not open a network connection, does not read credentials, and does not write to Gmail. Unknown export shapes fail closed.

## 1. What it is
A local-first, read-only archive of your mail. Phase 1 is a Python prototype: a SQLite schema, an idempotent import of a gog export, FTS5, a JSON CLI, and fixture tests. This revision adds schema v1. Import, FTS search, and the CLI are not in this tree.

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
mailcrawl/contract.py            # fail-closed export gate only
mailcrawl/schema.py              # schema v1 DDL
mailcrawl/store.py               # open one account database and migrate it
tests/
testdata/fixtures/gog-export/    # synthetic; contract mailcrawl.gog-export.v0
testdata/catalogue/              # synthetic taxonomy stand-in
skills/mailcrawl/SKILL.md
.cursor/skills/verify-mailcrawl/
```
Phase 1 language is Python. A Go port is later, after the prototype settles. This revision does not add a Go module.

## 4. Phase 1 contract
Phase 1 is read-only Python and it does all of the following:

- Create SQLite schema v1 for one local archive. `mailcrawl/schema.py` migrates an empty database to `user_version` 1.
- Import a gog export idempotently on `(account_id, gmail_message_id)`. That command is not in this revision.
- Index text with FTS5 using the tokenizer `unicode61 remove_diacritics 2`. Schema v1 creates `msg_fts`. The search command is not in this revision.
- Speak JSON on the CLI. That command is not in this revision.
- Test against synthetic fixtures only.

`mailcrawl.contract.classify_export` still returns `unsupported_schema` for every document, including a document labeled `mailcrawl.gog-export.v0`. That label is a name for the fixture. It is not a map of real gog field names. The import change replaces this gate with the rule in section 6 point 4.

## 5. Scaffold proof (this revision)
A scaffold change is done when, on a clean Linux runner with no network credentials and a temporary `HOME`, this exits 0:

```
make check && scripts/proof.sh
```

`scripts/proof.sh` uses a temp `HOME`, blocks proxy egress, and reads only the repository. It must show:

1. Required docs and the MIT `LICENSE` are present.
2. Synthetic fixtures under `testdata/` are present, use `example.com`, and are not gitignored.
3. Local archive paths, SQLite files, real-mail exports, models, and secret files are ignored, and none of those are tracked.
4. Python tests that exist pass.
5. The fixture contract `mailcrawl.gog-export.v0` fails closed with `unsupported_schema`.

The scaffold proof does not import mail, build an FTS index, or run a product CLI.

## 6. Closed decisions
These points are closed.

1. Schema v1 (`user_version=1`) contains only the tables ingest and FTS write. Those tables are `accounts`, `labels`, `senders`, `threads`, `messages`, `headers`, `message_labels`, `attachments`, `cursors`, `import_runs`, `ledger`, and `msg_fts`. Decisions, plans, embeddings, and clips arrive in a later migration when code writes them. v1 does not create empty tables for them.
2. The account alias is `personal`. `pessoal` is an optional CLI input alias that selects `personal`. The database file is `personal.db`.
3. `provenance_hash` is the SHA-256 of five lines. The lines are `gog`, the stable alias, `gmail_message_id`, `raw_sha256`, and the mailcrawl version. The hash does not use `account_id`. The version is the package semver plus a short git SHA when git can see one, for example `0.1.0+abc1234`.
4. The fixture contract stays `mailcrawl.gog-export.v0` until a Codex export confirms live gog field names. The four required keys are message id, thread id, label ids, and date. A document that lacks any of those four fails closed with `unsupported_schema`. Other listed fields are optional and stay NULL when the export omits them. Unknown keys go to one raw JSON field. Fixture key names belong to this contract. They are not a guess of live gog names.
5. `body_text` is the `text/plain` part. When the message has no plain part, `body_text` is the HTML part converted to text. The stored text includes quotations and signatures. Quote and signature stripping is phase 2. Phase 2 keeps this original text.
6. `msg_fts` indexes `subject`, `body_text`, `from_hdr`, and `attachment_text`. The tokenizer is `unicode61 remove_diacritics 2`. `attachment_text` stays empty. `attachments.extract_status` stays `pending`. Addresses in the local index stay on the machine.
7. `init` creates one database per alias, named `<alias>.db`. It does not create `index.db` or `reads.db`.
8. Real label names and `taxonomy-account-maps.json` stay off this repository. Tests load `testdata/catalogue/taxonomy.synthetic.json`. A real catalogue is a local file passed with `--taxonomy` or the config key `taxonomy`.
9. `headers` stores an allow-list, in lower case. The names are `list-unsubscribe`, `list-unsubscribe-post`, `list-id`, `precedence`, `auto-submitted`, `x-mailer`, `feedback-id`, `message-id`, `in-reply-to`, `references`, `reply-to`, `return-path`, `authentication-results`, and `received-spf`. Other headers are dropped.
10. `snippet` and `gmail_history_id` are NULL when the export omits them. `subject_norm` trims the subject, removes a leading run of `Re:`, `Fwd:`, and `Fw:` without regard to case, and collapses whitespace. It does not fold accents.

## 7. Scope
**In this revision:** spec, README, security policy, contributing guide, `make check`, CI, synthetic fixtures, scaffold proof, fail-closed contract gate, and schema v1.

**Still to land in phase 1:** idempotent import, FTS search, and the JSON CLI from section 4.

**Out of scope:** embeddings, Gmail writes, running `gog`, a language model on the crawl path, a live gog field map, and the Go port.
