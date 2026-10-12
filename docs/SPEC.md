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
mailcrawl/mbox.py                # streaming Takeout mbox reader
mailcrawl/extract.py             # attachment parser interface
mailcrawl/rules.py               # rules 0–8, no model
mailcrawl/taxonomy.py            # default taxonomy and local override
mailcrawl/report.py              # archive counts and dry-run counts
mailcrawl/schema.py              # schema v2 DDL
mailcrawl/store.py               # open one account database and migrate it
mailcrawl/ingest.py              # idempotent gog-export and mbox import, one writer
mailcrawl/search.py              # FTS search
mailcrawl/cli.py                 # init, import, and search
tests/
testdata/fixtures/gog-export/    # synthetic; v0 fixture contract and native gog layout
testdata/fixtures/mbox/          # synthetic; Takeout-style mbox and a gog export of two of its messages
testdata/catalogue/              # synthetic taxonomy stand-in
skills/mailcrawl/SKILL.md
.cursor/skills/verify-mailcrawl/
```
Phase 1 language is Python. A Go port is later, after the prototype settles. This revision does not add a Go module.

## 4. Phase 1 contract
Phase 1 is read-only Python and it does all of the following:

- Create SQLite schema v2 for one local archive. `mailcrawl/schema.py` migrates an empty database to `user_version` 2. A database at `user_version=1` gains `body_text_raw`, a cleaned `body_text`, a rebuilt `msg_fts`, and the empty tables `decisions`, `sender_decisions`, and `thread_mutes`.
- Import a gog export idempotently on `(account_id, gmail_message_id)`. `mailcrawl import gog-export` does that for the fixture contract and for a gog backup plaintext export.
- Import a Google Takeout mbox idempotently with `mailcrawl import mbox <file> --alias <thehive|vcl|personal>`. It streams the file and writes through the same writer as `import gog-export`. See section 4.1.
- Index text with FTS5 using the tokenizer `unicode61 remove_diacritics 2`. Import writes `msg_fts` from the cleaned body. `mailcrawl search` reads it.
- Parse attachment bytes through `mailcrawl.extract`. `text/plain` and `text/html` are extracted. Other types stay behind that interface. No external binary is required.
- Apply rules 0 to 8 with `mailcrawl rules`. `rules_version` is `rules-v0`. The command prints counts only.
- Print archive counts with `mailcrawl stats` and current-decision counts with `mailcrawl dry-run`. Both commands print counts only. Dry-run does not write a plan file and does not touch Gmail.
- Speak JSON on the CLI. `init`, `import`, and `search` accept `--json` before the command.
- Test against synthetic fixtures only.

`mailcrawl.contract.classify_export` returns `ok` for a `mailcrawl.gog-export.v0` document that has the four required keys from section 6 point 4. It returns `unsupported_schema` when the schema label differs or any of those keys is missing. The key names belong to the fixture contract. They are not a map of live gog field names.

`import gog-export` also reads a gog backup plaintext export when the directory has no `export.json`. The gogcli source writes `gmail/<account>/labels.json` and `gmail/<account>/messages/index.jsonl`. Each index record's `eml` value is a slash-separated path relative to the `gog backup export --out` directory. Pass that directory as `--dir`, or pass `gmail/<account>`. Paths that leave the export directory are rejected. A missing `.eml` file is `missing_file` and exits 1. A malformed record is `malformed_record` and exits 3. An unrecognized directory is `unsupported_schema` and exits 3. None of those errors include a message body or an address. A native gog import of a real thehive export was verified on the Mac at commit `32b936c`. The export had 25 messages. The first import counted 25 new, 0 duplicates, and 0 errors. The second import counted 0 new, 25 duplicates, and 0 errors. Two searches each returned 13 hits. Both searches returned the same `gmail_message_id` values. The database stayed at 25 messages. The synthetic fixture under `testdata/fixtures/gog-export/native/` is still the behavior this repository checks.

### 4.1 Takeout mbox import
The plan is a large initial archive from a Google Takeout mbox, then incremental imports through the gog API. That split keeps API quota and cost down.

```
mailcrawl --json import mbox <file> --alias <thehive|vcl|personal> [--labels <labels.json>] [--taxonomy <catalogue.json>]
```

`--alias` and `--account` are synonyms on both import commands. `pessoal` still selects `personal`. The import is read-only toward the file and toward Gmail. It reads one message at a time and never loads the whole file. It never holds a whole message either: only the header block is buffered, and the body goes to the hash and the email parser in 64 KiB chunks. Base64 attachments are decoded straight from the parsed text. Peak memory follows the largest single message, not the file size or the message count. A test bounds peak process RSS growth below four times the largest message. When a message's key is already stored, its body is hashed but not parsed, because a duplicate, a conflict, and a row from another source need only the header block and the hash. A reimport therefore stays near the baseline, and a test bounds its RSS growth below a quarter of the first import's. Every connection sets `PRAGMA mmap_size = 0`, because some SQLite builds map the database file by default and mapped pages count as process RSS. It writes schema v2 through the `import gog-export` writer, so body cleaning, attachment parsing, headers, and FTS are the same. There is no new migration. A file whose first non-blank line is not an mbox `From ` line is `unsupported_schema` and exits 3. A missing file is `missing_file` and exits 1. The JSON result adds `n_refused` and `n_reconciled` to the existing counts on both import commands.

**Takeout format status.** A real thehive Takeout was imported locally on 2026-10-11. It had 2,055 messages. The import and a reimport were idempotent, and nothing was refused. A fact marked *observed on thehive, 2026-10-11* below was seen in that archive. Everything else is still unverified. The parser is tolerant. A missing or odd value falls back to the next rule and never stops the import.

- *Observed on thehive, 2026-10-11.* The separator is `From <N>@<X> <X>`, with a decimal number before `@`. All 5 sampled separators had that shape. The file had 2,055 separators, and 2,055 messages were imported.
- *Observed on thehive, 2026-10-11.* Takeout writes `X-GM-THRID`, `X-Gmail-Labels`, and `X-Gmail-Original-Message-ID`. It does not write `X-Gmail-Message-ID`. `X-GM-MSGID` was not reported either. Also present were `X-Gm-Features`, `X-Gm-Gg`, `X-Gm-Message-State`, `X-Gm-Phishy`, `X-Gm-Spam`, `X-Get-Message-Sender-Via`, `X-Google-DKIM-Signature`, `X-Google-Id`, `X-Google-Notification-Metadata`, `X-Google-Sender-Auth`, `X-Google-Smtp-Source`, `X-Google-Calendar-Notification`, and `x-gong-email-entity`.
- *Observed on thehive, 2026-10-11.* 36 body lines started with `>From `. Line endings were mixed: 28,270,282 lines ended in CRLF and 724 in LF.
- *Observed on thehive, 2026-10-11.* The Portuguese system label names marked observed in the table below.
- *Unproven.* `X-Gmail-Original-Message-ID` is not a Gmail id. It may be the RFC 822 `Message-ID`. It is never used as a message id or a thread id until that is proven.
- *Unproven.* Which `X-Gm-*`, `X-Gmail-*`, and `X-Google-*` headers Google also writes into delivered mail, and so into the Gmail API raw that gog saves as `.eml`. The hash rule below assumes gog's `.eml` has none of them. If it does carry one, the two hashes differ and the rules under Reimport and reconciliation apply.
- *Observed on thehive, 2026-10-11.* gog's `.eml` files for the same mailbox had mixed line endings too: 28,259,588 lines ended in CRLF and 724 in LF. gog's `.eml` is hashed as written, with no line-ending change.
- *Proven on thehive, 2026-10-11.* Takeout-versus-gog reconciliation by message id. See Thehive reconciliation below for exactly what that covers.
- *Not proven.* Hash equality between Takeout and gog. It held for 421 messages and failed for 1,634. See Thehive reconciliation below.
- *Unverified.* The `X-GM-MSGID` and `X-Gmail-Message-ID` rules under Message identity come from the approved plan. Neither header was seen on thehive.

Splitting. A message starts at a line that begins with `From ` when that line is the first line of the file or follows a blank line. A body line that matches `>+From ` loses one `>`. The blank line before the next separator is dropped.

Message identity. `gmail_message_id` is lowercase hex with no padding, the same form as gog API ids. The first value that parses wins:

1. `X-GM-MSGID`, read as decimal.
2. `X-Gmail-Message-ID`. An all-digit value is read as decimal. A value with hex letters, or a `0x` prefix, is read as hex.
3. The decimal number before `@` on the `From ` separator line.

An id must be between 1 and 2^64 − 1. When no Gmail id parses, the key is `rfc822:` plus the SHA-256 of the `Message-ID` header with surrounding spaces and angle brackets removed. When there is no `Message-ID` either, the message is refused and counted in `n_refused`. A refused message is never merged. The ledger records its byte offset and the reason.

An `rfc822:` row is never merged with a gog API row automatically. It keeps `rfc822_message_id`, which is the key for a later reconciliation. That merge is future work and needs approval.

Thread identity. `X-GM-THRID`, read as decimal and stored as lowercase hex. Without it the thread is provisional, `mbox:` plus the SHA-256 of a root id. The root is the first `<…>` id in `References`, else in `In-Reply-To`, else the message's own `Message-ID`, else its key.

`raw_sha256` is computed over the message with the Takeout parts removed: the `From ` separator line and every header whose name starts with `X-Gm-`, `X-Gmail-`, or `X-Google-`, without regard to case, folded lines included. That covers `X-GM-THRID`, `X-Gmail-Labels`, `X-Gmail-Original-Message-ID`, and every `X-Gm-*` and `X-Google-*` header listed above. `X-Get-Message-Sender-Via` and `x-gong-email-entity` are kept, because they do not carry a Google prefix. Every line ending, CRLF or LF, becomes CRLF before hashing, so a file with mixed endings hashes the same as one with a single kind. The `>From ` unescape happens first. The parser reads the same bytes. Body text from either import loses its CR characters before body cleaning, so stored bodies and FTS do not depend on line endings. `raw_path` is `mbox:<absolute file path>#<byte offset of the separator>`. `internal_date` is the `Date` header, else the separator date, else 0.

A database written by an earlier version hashed Takeout messages without these rules. A reimport of the same file into that database meets different hashes. It counts those messages in `n_error` and leaves the stored rows unchanged. Start from a fresh account database to get the new hashes.

Labels. `X-Gmail-Labels` is a comma-separated list. Names may be quoted. Each name maps in this order:

1. A system name maps through the fixed table below, without regard to case. The label row uses the system id as both id and name, as gog `labels.json` does.
2. `Opened`, `Aberto`, and `Aberta` map to the pseudo-label `pseudo:opened`. `Archived`, `Arquivado`, and `Arquivada` map to `pseudo:archived`. See Pseudo-labels below.
3. A user name that matches a label name exactly, either in the file passed with `--labels` (a gog `labels.json`) or in a non-provisional label already in the account database, uses that label id.
4. Any other name is a provisional user label with id `mbox:<name>`.

| Gmail id | Names (EN and PT) | PT on thehive, 2026-10-11 |
| --- | --- | --- |
| `INBOX` | Inbox, Caixa de Entrada | Caixa de Entrada observed |
| `SENT` | Sent, Sent Mail, Enviados, Enviadas, E-mails enviados | not observed |
| `STARRED` | Starred, Com estrela | not observed |
| `IMPORTANT` | Important, Importante | Importante observed |
| `UNREAD` | Unread, Não lido, Não lidos, Não lida, Não lidas | Não lidas observed |
| `DRAFT` | Draft, Drafts, Rascunho, Rascunhos | Rascunhos observed |
| `SPAM` | Spam | Spam observed |
| `TRASH` | Trash, Lixo, Lixeira, Reciclagem | not observed |
| `CHAT` | Chat, Chats | not observed |
| `CATEGORY_PERSONAL` | Category Personal, Categoria Pessoal | Categoria Pessoal observed |
| `CATEGORY_SOCIAL` | Category Social, Categoria Social, Categoria Redes sociais | not observed |
| `CATEGORY_PROMOTIONS` | Category Promotions, Categoria Promoções | Categoria Promoções observed |
| `CATEGORY_UPDATES` | Category Updates, Categoria Atualizações | Categoria Atualizações observed |
| `CATEGORY_FORUMS` | Category Forums, Categoria Fóruns | not observed |

Each Gmail id in the table also maps to itself. Names are compared after Unicode NFC normalization. A Portuguese name marked not observed is a guess until a real sample shows it. The English names were not checked on real mail.

Pseudo-labels. `pseudo:archived` means the message had no `INBOX` label when Takeout exported it. `pseudo:opened` means it had no `UNREAD` label. They come from Takeout only. They are not Gmail labels and are never sent to Gmail. The reserved `pseudo:` prefix on `labels.gmail_label_id` is what marks them as non-Gmail. The label row uses the id as its name and `kind` `system`. `kind` cannot carry a third value without rebuilding the `labels` table, so there is no migration. Rules 0 to 8 and dry-run read labels only through `mailcrawl.contract.GMAIL_LABEL_SQL`, which excludes `pseudo:` ids. Any later plan or Gmail write path must read labels the same way. A pseudo-label never gets catalogue fields. Only an mbox import writes or replaces a message's `pseudo:` labels. A gog import keeps them, ignores any `pseudo:` id in its own export, and does not count them toward `n_error` or `n_reconciled`, because `X-Gmail-Labels` is outside `raw_sha256`. Provisional `mbox:` label replacement leaves `pseudo:` rows alone. A pseudo-label is a Takeout snapshot. A later gog import does not recompute it from `INBOX` or `UNREAD`.

Reimport and reconciliation. The key stays `(account_id, gmail_message_id)`, so a second import of the same file adds no rows and refreshes labels. Later imports follow these rules:

- A gog import that meets an mbox row with the same id keeps the row. When the hashes match it counts `n_dup`. When they differ it counts `n_reconciled` and appends `{gmail_message_id, kept_raw_sha256, seen_raw_sha256}` to the ledger. In both cases it replaces the message's labels with the gog label ids. It moves the message from a provisional `mbox:` thread to the real thread id. There is never a second message row.
- A gog import whose `labels.json` has a label named `<name>` replaces the provisional label `mbox:<name>` everywhere, on messages that the gog export does not cover too.
- A provisional thread with no messages, no decision, and no mute is deleted at the end of an import.
- An mbox import that meets a row from another source counts `n_dup` and leaves the row and its labels alone.
- Two gog imports whose bytes differ for one id still count `n_error` and keep the stored row, as before.

`n_reconciled` is a state count, not an event count. It counts messages in this gog export whose stored row came from an mbox import and whose stored `raw_sha256` differs from the gog copy. The kept mbox row, with its `raw_sha256` and `mbox:` `raw_path`, is never rewritten. So every later gog import of the same messages counts them in `n_reconciled` again and appends them to the ledger again. That is the specified behavior, not a counter bug. A test pins it. Only the stored row decides between `n_dup` and `n_reconciled`. The ledger is never consulted.

Thehive reconciliation, 2026-10-11. The Pit imported the thehive Takeout and then a gog export of the same mailbox on the Mac, at `main` with #17 and #18. Only counts are recorded here.

| Step | n_new | n_dup | n_reconciled | n_error |
| --- | --- | --- | --- | --- |
| Takeout import | 2,055 | 0 | 0 | 0 |
| Takeout reimport | 0 | 2,055 | 0 | 0 |
| gog import | 4 | 421 | 1,634 | 0 |
| gog reimport | 0 | 425 | 1,634 | 0 |

After the gog import the account had 2,059 messages with 2,059 distinct ids. Threads went from 1,275 to 1,278 and labels from 49 to 117. No Takeout message was missing from the gog export. 4 gog messages were missing from the Takeout. 34 `mbox:` labels were converted to gog label ids, and 0 provisional label links were left. 5 catalogue entries matched no label. `pseudo:opened` stayed on 604 messages and `pseudo:archived` on 13.

What that proves: matching by message id. Every Takeout message met its gog copy under the same id, with no duplicate row. The decimal separator id, read as hex, equals the gog API id. Provisional `mbox:` labels and threads were replaced. The reimport counts match the state-count rule above: 425 = 421 + 4 rows now stored from gog.

What it does not prove: hash equality. 1,634 of the 2,055 shared messages had a Takeout `raw_sha256` that differed from the gog `raw_sha256`. The cause is open. These are hypotheses only. Each mechanism is shown on synthetic data in `tests/test_mbox.py` (`test_hash_mechanisms_between_takeout_and_gog`), but none is shown to be what happened on thehive:

- Hypothesis: delivery headers on one side only. Takeout drops every `X-Gm-*`, `X-Gmail-*`, and `X-Google-*` header. gog's `.eml` is hashed as written. If gog's raw carries one of those headers, such as `X-Gm-Message-State` or `X-Google-Smtp-Source` from the sending side, the hashes differ. Headers that one side adds or re-folds would differ in the same way.
- Hypothesis: line endings. Takeout lines become CRLF before hashing. gog lines do not. A message whose own raw has an LF-only line hashes differently. Both sides had 724 LF lines, so this can explain at most 724 messages.
- Hypothesis: `>From ` escaping. If Takeout writes mboxo (an original `>From ` line stays `>From `), the unescape turns it into `From ` and the hashes differ. Under mboxrd (`>>From `) they match. 36 such lines can explain at most 36 messages.

Checking a hypothesis needs a per-message comparison of sanitized header names and line-ending counts on the Mac. Real values never leave the Mac.

### 4.2 Default taxonomy
The default taxonomy is Gmail's own categories. System labels are `INBOX`, `SENT`, `STARRED`, `IMPORTANT`, `UNREAD`, `DRAFT`, `SPAM`, `TRASH`, and `CHAT`. Category tabs are `CATEGORY_PERSONAL`, `CATEGORY_SOCIAL`, `CATEGORY_PROMOTIONS`, `CATEGORY_UPDATES`, and `CATEGORY_FORUMS`. A label with one of those ids stores that id as its category key. No other content type is invented. A label id that starts with `pseudo:` is never a category.

Rules 2, 2-read, 3, and 4 need content types that are not in that default. They do not fire and they do not guess. `dry-run` lists them in `inactive_rules` and sets `inactive_reason` to `gmail_default`. `stats` counts messages in `by_category` and does not apply those rules.

Two customizations use one JSON file. Lookup order is the `--taxonomy` flag, then the environment variable `MAILCRAWL_TAXONOMY`, then the config key `taxonomy`, then `~/.config/mailcrawl/taxonomy.json`. Sections are `thehive`, `vcl`, `personal`, and an optional `default`. `pessoal` is accepted and means `personal`. An account section replaces that category's `default` list. Each value is a list of label names.

Map a custom label onto a Gmail category:

```json
{"default": {"CATEGORY_PROMOTIONS": ["Example type/Bulletin"]}}
```

That keeps `inactive_reason` at `gmail_default`.

Or opt into another schema. The keys in the file are the schema. `testdata/catalogue/taxonomy.synthetic.json` is an example, not the default. It maps example label names to `newsletter`, `job_alert`, `notification`, `invoice_receipt`, `purchase_delivery`, `account_security`, `contract`, `correspondence`, `human`, and `automated`. When those keys are present, the matching content-type rules can fire. `dry-run` leaves `inactive_rules` empty. A schema that omits some of those keys lists the rules that still cannot fire and sets `inactive_reason` to `schema`.

```json
{
  "default": {"newsletter": ["Example type/Bulletin"]},
  "thehive": {"invoice_receipt": ["Example type/Receipt"]}
}
```

Invalid JSON, a file that is not an object, an unknown account, or a value that is not a list of label names exits 2. A label name the account does not have is counted in `taxonomy_warnings` and listed in `missing_labels`. The command still exits 0.

`*.local.json` and a `taxonomy.json` at the repository root are gitignored. The example schema contains example label names only.

The rule roles `finance`, `purchase`, and `brief` are `invoice_receipt`, `purchase_delivery`, and `correspondence`. `rules_version` stays `rules-v0`. Those rules fire when the opted-in schema supplies the key.

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
15. On that same account, `mailcrawl stats` prints 2 messages, 2 threads, 2 senders, and month `2023-11` with count 2. `mailcrawl dry-run` prints state counts `needs_review` 1 and `reply` 1, rule counts `0` 1 and `5` 1, label `STARRED` once, sender ids `1` and `2` once each, and month `2023-11` with count 2. Neither command prints a message body or an address. The decision count stays 2. No `plans` table is created.
16. The synthetic Takeout mbox imports 5 messages and refuses 1. A second import adds nothing. Four of those messages carry a `pseudo:opened` or `pseudo:archived` label. A gog import of two of those messages afterwards keeps 5 rows, counts 1 duplicate, 1 reconciled, and 0 errors, leaves no provisional label, and keeps all four pseudo-label links.

## 6. Closed decisions
These points are closed.

1. Schema v1 (`user_version=1`) contained the ingest and FTS tables `accounts`, `labels`, `senders`, `threads`, `messages`, `headers`, `message_labels`, `attachments`, `cursors`, `import_runs`, `ledger`, and `msg_fts`. A database at `user_version=1` migrates to schema v2. Schema v2 adds `body_text_raw` and the empty tables `decisions`, `sender_decisions`, and `thread_mutes`. It does not create `plans`, `proposals`, embeddings, or clips.
2. The account alias is `personal`. `pessoal` is an optional CLI input alias that selects `personal`. The database file is `personal.db`.
3. `provenance_hash` is the SHA-256 of five lines. The lines are `gog`, the stable alias, `gmail_message_id`, `raw_sha256`, and the mailcrawl version. The hash does not use `account_id`. The version is the package semver plus a short git SHA when git can see one, for example `0.1.0+abc1234`.
4. The fixture contract stays `mailcrawl.gog-export.v0`. The four required keys are message id, thread id, label ids, and date. A document that lacks any of those four fails closed with `unsupported_schema`. Other listed fields are optional and stay NULL when the export omits them. Unknown keys go to one raw JSON field. Fixture key names belong to this contract. They are not gog field names. The gog plaintext export is a second layout, read from gogcli `gmailExportIndexEntry` and `gmailbackup.Label`. Index fields are `id`, `threadId`, `historyId`, `internalDate`, `labelIds`, `sizeEstimate`, `subject`, `from`, `to`, `cc`, `date`, and `eml`. `to` and `cc` are arrays of strings. `internalDate` is unix milliseconds and is stored as unix seconds. `labels.json` is an array. Each entry uses `id`, `name`, and `type` (`system` or `user`). Message ids, thread ids, and label names are stored as written. A record needs `id`, `threadId`, and either `internalDate` or `date`, plus an `eml` path. `labelIds` may be absent. Unknown index keys go to the same raw JSON field. `markdown` and `attachments` are known and are not a second body. Markdown-only records, which have no `eml`, are rejected.
5. `body_text_raw` is the phase 1 text, untouched. That text is the `text/plain` part, or the HTML part converted to text when the message has no plain part. `body_text` is that text after cleaning. Cleaning removes a line that starts with `>` once leading spaces are ignored. It also cuts from the first clear marker through the end of the text. The markers are a line `On … wrote:`, a line `Em … escreveu:`, a line `-----Original Message-----`, an Outlook header block that starts at `From:` or `De:` when `Sent:` or `Enviada:` appears within the next eight lines, and a signature that starts at a line that is exactly `-- ` (two dashes and a space). A line that is `--` with no space stays. Patterns are Portuguese and English only. When unsure, do not cut. The `.eml` file at `raw_path` stays the source of truth. A v2 migration copies the old `body_text` into `body_text_raw` and then cleans `body_text`.
6. `msg_fts` indexes `subject`, the cleaned `body_text`, `from_hdr`, and `attachment_text`. It does not index `body_text_raw`. The tokenizer is `unicode61 remove_diacritics 2`. A `text/plain` or `text/html` part with bytes stores the decoded text, `extract_status` `ok`, and `file_sha256`. A part with no bytes is `skipped` with reason `no_bytes` and a NULL hash. PDF, image OCR, Office, and `ics` are `skipped` when their backend is absent. Any other type is `unsupported`. Skipped and unsupported parts are not indexed as text. Addresses in the local index stay on the machine.
7. `init` creates one database per alias, named `<alias>.db`. It does not create `index.db` or `reads.db`.
8. Real label names and `taxonomy-account-maps.json` stay off this repository. The default taxonomy and the local override are in section 4.2. Tests load `testdata/catalogue/taxonomy.synthetic.json`. A real override stays in a local file.
9. `headers` stores an allow-list, in lower case. The names are `list-unsubscribe`, `list-unsubscribe-post`, `list-id`, `precedence`, `auto-submitted`, `x-mailer`, `feedback-id`, `message-id`, `in-reply-to`, `references`, `reply-to`, `return-path`, `authentication-results`, and `received-spf`. Other headers are dropped.
10. `snippet` and `gmail_history_id` are NULL when the export omits them. `subject_norm` trims the subject, removes a leading run of `Re:`, `Fwd:`, and `Fw:` without regard to case, and collapses whitespace. It does not fold accents.
11. Attachment parsing handles `text/plain` and `text/html` only. PDF, OCR, Office, `ics`, `attachment_fields`, and image embeddings stay behind an interface with no required implementation. An unsupported type is `unsupported`. A missing backend is `skipped`. A row with no `file_sha256` is `skipped` with reason `no_bytes`. OCR thresholds are v0 constants: fewer than 25 characters per page tries OCR, and `ocr_conf` below 60 is `low_confidence`. Those thresholds are exercised only when a backend is present. This revision adds that interface in `mailcrawl/extract.py`. The default backend does not invoke `pdftotext` or `tesseract`. Image embeddings and `attachment_fields` are not created.
12. The default taxonomy is the Gmail categories in section 4.2. Content-type rules stay off until an override schema supplies their keys. `invoice_receipt`, `purchase_delivery`, and `correspondence` replace `finance`, `purchase`, and `brief` inside that example schema. Real label names never appear in code or tests. They stay in a local file outside Git.
13. Guards run first. A guard can only force `needs_review` or block `noise` and `resolved`. Later rules choose the other states. `STARRED` and `IMPORTANT` are Gmail system label ids. Rules never read a `pseudo:` label.
14. These definitions are v0, tunable, and marked v0 in code. A human participant is a sender whose origin label is human. When that label is missing, the sender has no `List-Unsubscribe`, no `Precedence` of `bulk` or `list`, and no `Auto-Submitted` other than `no`. The label beats the header. Unrecognized Account & Security means a sender with no `sender_decisions` row. The read-list is `sender_decisions.route = 'read'`. There is no extra domain file. `read_later_expiry_days` is 30, and expiry is tested on fixtures. Open questions, concrete tasks, and closing language are short clue lists in Portuguese and English. They live in a configuration file, not in the engine. The minimum question clues are a `?` at the end of a sentence outside a quote, or a start of `can you`, `could you`, `podes`, `consegues`, or `pode`. The minimum task clues are `please`, `por favor`, `preciso que`, `need you to`, and `can you send`. The minimum closing clues are `thanks`, `obrigado`, `cumprimentos`, `best regards`, `perfect`, and `combinado`. That list is not final. The client plus-three-business-days path is unimplemented. Everyone uses plus seven days. `sender_decisions.client_sla_days` exists and is NULL by default. `sender_decisions.route` may be `imbox`, `feed`, `paper_trail`, or `read`. Priority comes from `sender_decisions.priority`. When that row is missing, priority is NULL.
15. Coverage is the fraction of threads with a current decision. `needs_review` counts as covered. The report is counts by `next_state` and by `rule_id`. A sample of 100 and a 2% threshold are not a fixture gate.
16. Dry-run prints counts only: state, label, sender, month, and rule. It does not write a plan file and it does not touch Gmail. Plans are phase 4.
17. `rules_version` is `rules-v0`. A change to a rule, a constant, or a list increments it. `evidence` stores ids and rule or label names only, never email text.
18. No language model runs on the crawl. The first matching rule wins. There is no Gmail write, no network, and no `gog`.

## 7. Scope
**In this revision:** spec, README, security policy, contributing guide, `make check`, CI, synthetic fixtures, schema v2, body cleaning, the attachment parser interface, rules 0 to 8, `stats`, dry-run counts, `init`, idempotent `import gog-export` for the fixture contract and for a gog backup plaintext export, a streaming `import mbox` for a Takeout mbox, FTS search over the cleaned body, and the phase 1 proof.

**Out of scope:** Embeddings, label apply, Gmail writes, running `gog`, a language model on the crawl path, a full thehive backup, cache deletion, markdown-only gog exports, and the Go port stay out. Section 4 records a 25-message thehive smoke import. A full mailbox import is not this revision.

## 8. Later phases
Body cleaning, schema v2, the attachment interface, rules 0 to 8, `stats`, and dry-run counts are in this revision. Sections 4 to 7 describe that work.

An inference stays marked as still to be decided. An open approval stays marked as still to be decided.

### Phase 2. Cleaned text, parser interface, rules, and counts
Body cleaning, the attachment interface, rules 0 to 8, `stats`, and dry-run counts are done. `rules_version` is `rules-v0`. There is no model. Dry-run prints counts only. It writes no plan file and it does not touch Gmail.

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
- Merging an `rfc822:` mbox row with a gog API row by `rfc822_message_id`. That merge needs approval.
- The Takeout mbox facts in section 4.1 that are not marked observed or proven on thehive, 2026-10-11, and hash equality between Takeout and gog (1,634 of 2,055 differed on thehive).

Section 6 point 4 still rejects a record with no `eml` path.
