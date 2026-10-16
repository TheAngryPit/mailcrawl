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
- **Deterministic first.** Inference runs only at a point this spec declares. Each declared point is config-gated and off by default.
  - No inference runs in quote and signature stripping, sender rules, label rules, the `image_only` flag, `pdftotext`, database backfill, migrations, stats, or a read of a stored decision.
  - The declared points are embeddings and nearest neighbour, including the search query embedding in its own table, the Jev question battery, the decision comparison, the OCR pilot, the experimental search reranker, and a future language model for the low-confidence band.
  - The battery is the only point that calls Jev for classification. Its stored answers feed the decision comparison, the grey band, lane ordering, and extraction. Reading a stored answer is not inference.
  - The search reranker is one Noul per query and thread on the top 20, and it is off by default.
  - The decision comparison is logistic regression, Laya, SetFit, and the Jev candidates below.
  - Every stored decision records its source. The source is `rule`, `knn`, `model`, or `human`.
  - A test fails when code calls a model outside a declared point. The scan reads Python syntax. It flags `predict`, `embed`, `generate`, `complete`, and `systemone` unless that call is the callable passed to a declared point. It flags an import of a known model client (`openai`, `anthropic`, `transformers`, `sentence_transformers`, `torch`, `ollama`, `setfit`, `httpx`, `requests`, `aiohttp`, `huggingface_hub`). It does not see `getattr`, `exec`, a renamed binding, or a client loaded from a string. The crawl path still runs no model.
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

- Create SQLite schema v2 for one local archive. `mailcrawl/schema.py` migrates an empty database to `user_version` 5. A database at `user_version=1` gains `body_text_raw`, a cleaned `body_text`, a rebuilt `msg_fts`, and the empty tables `decisions`, `sender_decisions`, and `thread_mutes`. A database at `user_version=2` migrates to `user_version` 3. That step adds `screener` to the decisions state check and does not add a table. A database at `user_version` 3 gains `origin` and `source_detail` on `decisions` and becomes `user_version` 4. `origin` is `rule`, `knn`, `model`, or `human`. `source_detail` records the rule id or the model id. `created_at` is the timestamp. A current row is not inferred again. A database at `user_version` 4 gains `threads.image_only` and `msg_trigram` and becomes `user_version` 5.
- Import a gog export idempotently on `(account_id, gmail_message_id)`. `mailcrawl import gog-export` does that for the fixture contract and for a gog backup plaintext export.
- Import a Google Takeout mbox idempotently with `mailcrawl import mbox <file> --alias <thehive|vcl|personal>`. It streams the file and writes through the same writer as `import gog-export`. See section 4.1.
- Index text with FTS5 using the tokenizer `unicode61 remove_diacritics 2` on `msg_fts`, plus `msg_trigram` with the trigram tokenizer. Import writes both from the cleaned body and extracted attachment text. `mailcrawl search` applies SQL filters, then `msg_fts`, then trigram hits the word index missed.
- Parse attachment bytes through `mailcrawl.extract`. `text/plain` and `text/html` are extracted. Other types stay behind that interface. No external binary is required. When `pdftotext` is on PATH, PDF bytes are extracted with it. A missing binary still skips the PDF. `tesseract` is not invoked.
- Apply rules 0 to 8 with `mailcrawl rules`. `rules_version` is `rules-v4`. The command prints counts only.
- Print archive counts with `mailcrawl stats` and would-be decision counts with `mailcrawl dry-run`. Both commands print counts only. Dry-run evaluates rules and does not write decisions. Neither command writes a plan file or touches Gmail.
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

An archive imported before those keys existed has the Gmail label id stored and `catalogue_key` empty. `backfill` copies the id into `catalogue_key` for the system labels and category tabs above. It does not change a key that is already set, it does not invent a content type, and it does not update message rows. A `pseudo:` id is skipped. `stats` and `dry-run` run the same fill before they count, so an old archive does not need a reimport. Running `backfill` twice changes nothing the second time. `testdata/fixtures/old-taxonomy/pre-category.sql` is the synthetic old archive.

`stats` reports `uncategorized` as the number of messages with none of the five category tabs. `dry-run` evaluates the current rules against the current taxonomy and does not read or write the `decisions` table. `by_rule` and `by_state` are the would-be decisions. `decisions_by_category` counts those decisions under each category the thread carries. `undecided` is the number of threads whose would-be rule is `else`. `--now` and `--rules` select the clock and the rules file. The default rules file is `testdata/rules/rules-v0.json`. Its `rules_version` is `rules-v4`.

The taxonomy file maps label names, not label ids. One label name may be listed under more than one category. A source label can be both `automated` and `notification`. `dry-run` and `stats` give the thread the union of those categories. A name in the file wins over a `catalogue_key` already stored, including a key left blank or set to something else. The count does not write `catalogue_key`. Import stores the key when the name has one category. When the name has several, import stores no single key. The union is applied when the taxonomy file is loaded. `dry-run` adds `override_matches`: each category in the file, with `messages` and `threads`. A message counts in every category its labels carry. That object has no label names. A label id, including a `mbox:` id, is not the lookup key.

The first matching rule still wins. Section 4.6 lists the `rules-v4` order. A label that is both `automated` and `notification` can match rule `2` when the thread is list mail. A label that is both `human` and `automated` does not match rule `5`. An `account_security` category that already matched an earlier rule stays on that earlier rule. `correspondence`, `human`, and `automated` do not by themselves block the Gmail tab fallback.

`--export-decisions` writes one local CSV row per thread. The columns are `gmail_thread_id`, `rule`, `lane`, `override_match`, `suggested_rule`, `suggested_lane`, and `suggested_confidence`. `override_match` is the matching category keys, sorted and joined with `|`, or blank when the thread matches none. The suggestion columns are blank unless `--advise` is set, and then only on a thread whose rule is `else`. Section 8 phase 7 describes that suggestion. It does not change `rule` or `by_rule`. The file has no label names. `*.csv` is gitignored. The JSON count stays the full archive and adds `exported` for the number of rows written. `--sample N` keeps every rule that fired, including `else` and `spam-trash`, then fills up to N from the rest. If N is smaller than the number of rules, the file still has one row per rule. `--seed` makes that choice repeatable and defaults to 0. `--sample` without the export path exits 2. The export does not write the database and does not touch Gmail.

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

The rule roles `finance`, `purchase`, and `brief` are `invoice_receipt`, `purchase_delivery`, and `correspondence`. `rules_version` is `rules-v4`. Those rules fire when the opted-in schema supplies the key.

### 4.3 Rule answers in rules-v1

Rule `0` keys only on the Gmail label id `STARRED`. A thread with `IMPORTANT` and no `STARRED` label does not match rule `0`. It continues at the next rule.

A thread with `SPAM` or `TRASH` that has not already matched rules `0` through `4` matches rule `spam-trash`. The state is `resolved`. The lane is `screened_out`. This rule runs before rules `5`, `6`, `7`, `8`, and `else`, so that thread does not become `reply` or `action`. A starred thread still matches rule `0` first. A muted thread still matches rule `1` first.

Rule `else` stays state `needs_review` on the decision row. `dry-run` does not put rule `0` and rule `else` in one `needs_review` count. Rule `0` is `needs_review_rule0`. Rule `else` is `needs_review_else`. `undecided` is still the `else` count. `stats` reports `needs_review_rule0` and `needs_review_else` the same way. Another rule that returns `needs_review`, such as rule `4`, keeps the state name `needs_review` in `dry-run`.

### 4.4 Rule answers in rules-v2

`rules_version` is `rules-v2`. The rules file path stays `testdata/rules/rules-v0.json`. Dry-run still does not write the `decisions` table. `--export-decisions` still writes the local CSV.

Owner aliases live in the local config, `~/.config/mailcrawl/config.toml` or the file named by `--config`. Each account section may list `owners` and `domains`. The account `email` already counts as from me at import. `owners` are extra addresses. A domain matches the part after `@`, compared in lower case. Mail from any of those counts as from me. It never matches rule `5`. It can match rule `6` (`waiting`) or rule `8` (`resolved`). A later `init` keeps `owners` and `domains`. A `config.toml` at the repository root is gitignored. `testdata/config/owners.synthetic.toml` is the synthetic example.

```toml
[accounts.thehive]
email = "ada@example.com"
owners = ["ada@example.com", "ada.other@example.com"]
domains = ["example.com"]
```

Automated mail beats a human category. A thread that has both a human category and an automated category, a last sender whose local part is `no-reply`, `no_reply`, `noreply`, or `notification`, or a transfer notice, does not match rule `5`. Transfer notices are the phrases `download is ready` and `transfer complete`.

Security protection is only real security mail. `account_security` is protected, and can match rule `4`, only when the thread has no list header and no `CATEGORY_PROMOTIONS` label. A list header or the promotions tab clears that protection, so rule `2` can match first.

The human test needs more than missing list headers. A no-reply or notification sender is not human. Receipt or invoice wording matches rule `3` with state `reference` and lane `paper_trail` even when the thread has no invoice label. The words are `invoice`, `receipt`, `fatura`, and `recibo`.

A thread whose every message carries `DRAFT` matches rule `draft` before rules `5` through `8`. The state is `resolved`. The lane is `screened_out`. It is not `waiting`.

Own housekeeping matches rule `housekeeping` before rules `5` through `8` and before `else`. A calendar response, or a message whose `To` addresses are all owner addresses or owner domains, is state `resolved` and lane `screened_out`. An empty `To` is not a message to self. The calendar phrases are `accepted this invitation`, `declined this invitation`, and `tentatively accepted`.

Automated reference matches rule `auto-ref` before rules `5` through `8` and before `else`, and only when the thread is not human. Meeting notes, a periodic report, a sharing notice, an expired invite, or a transfer notice is state `reference` and lane `paper_trail`. The phrases are `meeting notes`, `weekly report`, `monthly report`, `shared a document`, and `invitation has expired`, plus the transfer notices above. The same wording from a human sender stays on the human rules.

Rule `7` does not match list mail or promotions, even when the body has a task phrase.

A short closing from the other side matches rule `8` before rule `5`. The state is `resolved` and the lane is `imbox`. Short means the stripped body is at most 80 characters and at most two non-empty lines, and it matches a closing clue without a question or a task. A longer human reply that also contains a closing clue stays rule `5`. A short closing from me also matches rule `8` before rule `6`, so it is `resolved` and not `waiting`.

### 4.5 Rule answers in rules-v3

`rules_version` is `rules-v3`. The rules file path stays `testdata/rules/rules-v0.json`. Dry-run still does not write the `decisions` table. `--export-decisions` still writes the local CSV. `inactive_rules` still lists content rules `2`, `2-read`, `3`, and `4` on the Gmail default. The tab fallbacks below use those rule ids on a thread that has no custom content key.

Phrase lists live in the rules file under `phrases.en` and `phrases.pt`. The engine unions both locales with the top-level lists. `out_of_office` is part of the automated-reference list. Transfer checks use `transfer_notice` from both locales. The Portuguese additions include meeting notes (`notas da reunião`), weekly and monthly reports, file viewed or downloaded, an expired invite, out of office, and transfer notices.

`CATEGORY_PROMOTIONS` beats a human label. That thread is not human and does not match rule `5`. With no custom content key it matches rule `2`.

Receipt words match rule `3` only on an automated thread or a one-message thread. A multi-message human thread stays on the human rules. An invoice or purchase label still matches rule `3`.

Payment-failed and action-required wording matches rule `7`, state `action`, before rule `3`. The English clues include `payment failed` and `action required`. The Portuguese clues include `pagamento falhou` and `ação necessária`.

An auto-reply matches rule `auto-reply` before rule `7`. The state is `noise` and the lane is `feed`. It matches an `Auto-Submitted` header other than `no`, or a subject in the `auto_reply_subject` list, including `automatic reply` and `resposta automática`.

A mostly-HTML body sent by me matches rule `test-send`, state `resolved`, lane `screened_out`. Mostly-HTML means the stripped body is at least 40 characters and contains at least three HTML tags.

Portuguese question clues `diz-me se`, `diz me se`, `confirma`, and `achas que` count as questions, so a message from me that uses them matches rule `6` when it is not a short closing.

A thread with no custom content key uses the Gmail tab. `CATEGORY_UPDATES` plus security wording matches rule `4`. `CATEGORY_PROMOTIONS` matches rule `2`.

The first message from a sender, with no reply from me in that thread, and sales wording, matches rule `screener`. The state is `screener` and the lane is `imbox`. It does not match rule `5`. Sales clues include `special offer` and `proposta comercial`.

Sender local parts `no-reply`, `noreply`, `no_reply`, `notification`, `news`, `info`, `team`, and `newsletter` are automated even when a human label is present. A local part of `newsletter` matches rule `2`. A sender domain that starts with `mail.` is not human.

`List-Unsubscribe` or `Precedence: bulk` means the thread is not human, even with a human label.

An `account_security` category that has not already matched an earlier rule matches rule `4` instead of `else`.

`security_senders` in the account config is an allow-list of security-alert addresses. Mail from one of them matches rule `4`. The list is local. `testdata/config/owners.synthetic.toml` uses `alerts@example.com`. A later `init` keeps the list.

### 4.6 Rule answers in rules-v4

`rules_version` is `rules-v4`. The rules file path stays `testdata/rules/rules-v0.json`. Dry-run still does not write the `decisions` table. `--export-decisions` still writes `gmail_thread_id`, `rule`, `lane`, and `override_match`, plus the suggestion columns in section 4.2. The label map does not need an edit. One label may still sit in several categories, and the thread still carries that union.

A specific content key is `newsletter`, `job_alert`, `notification`, `invoice_receipt`, `purchase_delivery`, `account_security`, or `contract`. `correspondence`, `human`, and `automated` are not specific content keys. When none of the specific keys match, `CATEGORY_PROMOTIONS` matches rule `2` and `CATEGORY_UPDATES` matches rule `auto-ref` with state `reference` and lane `paper_trail`. Security wording on that updates tab still matches rule `4` first. `inactive_rules` is unchanged. It still lists rules `2`, `2-read`, `3`, and `4` on the Gmail default.

A thread that has both `automated` and `CATEGORY_UPDATES` does not end on `else`. It matches `auto-ref`.

A read-tracking notice inside a thread I sent does not change the decision. The engine ignores that message and classifies the earlier mail. The phrases include `viewed your file` and `visualizou o ficheiro`. A tracking notice with no message from me still follows the other rules.

Transfer mail is automated. Local parts `transfer` and `fileshare` match `auto-ref`. So does `files (3) from`, `files (3) to`, `arquivos (2) para`, and `arquivos (2) de`, with any count in the parentheses.

Rule `test-send` matches mail from me only when the body has at least 40 characters and at least three HTML tags, and either the body has no prose before the first HTML tag or `To` contains an address in `test_recipients`. A normal note with a question and an HTML signature stays rule `6`, state `waiting`. `test_recipients` lives in the account config. `testdata/config/owners.synthetic.toml` uses `tester@example.com`. A later `init` keeps the list.

Rule `8` does not match list mail or `CATEGORY_PROMOTIONS`. Those threads do not become `resolved` in lane `imbox`.

Rule `screener` matches while I have never sent in that thread. The sender may already have other threads, and the pitch may be a later message in the same thread. A reply from me keeps the thread off `screener`. Extra sales clues are `you are invited`, `partnership proposal`, `convite para o evento`, and `proposta de parceria`.

`overdue`, `unpaid`, `em atraso`, and `por pagar` match rule `7` before rule `3`. A usage or budget alert does the same. The clues include `budget alert` and `alerta de utilização`. When I sent the last message and it is a question, and the thread is not draft-only, the decision is rule `6` before rule `3`. A receipt from `no-reply`, including one whose subject is an auto-reply, stays rule `3`.

Portuguese task clues `é só`, `e so`, `troca`, `altera`, and `envia` are task phrases. Stale meeting notices `happening now` and `a decorrer agora` match `auto-ref` and are not human. Portuguese security notices `email de recuperação validado` and `exportação de dados` match rule `4` when the thread is not list mail and not on the promotions tab.

A file handoff from me with no question and no task matches rule `handoff`, state `resolved`, lane `screened_out`. The phrases include `here is the file` and `segue o ficheiro`. The same handoff with a question stays rule `6`.

An access request from an automated sender matches rule `7`, state `action`, before `auto-ref`. The phrases include `requesting access` and `pediu acesso`. A sharing notice that is not a request still matches `auto-ref`.

The first match wins, in this order. Rule `0`. Rule `1`. Rule `2-read`. Rule `2`, including a `newsletter` local part and a promotions tab with no specific content key. Billing, overdue, unpaid, and usage alerts as rule `7`. Rule `test-send`. A question from me as rule `6` when the thread is not draft-only. Rule `3`. Rule `4`, including a security notice and an updates tab with security wording. Rule `spam-trash`. Rule `draft`. Rule `housekeeping`. Rule `auto-reply`. An access request as rule `7`. Rule `auto-ref`, including a transfer, a stale meeting, and an updates tab with no specific content key. Rule `handoff`. A short closing as rule `8` when the thread is not list mail or promotions. Rule `screener`. Rule `5`. Rule `6`. Rule `7`. A longer closing as rule `8` on the same list-mail exclusion. An `account_security` category that missed rule `4`. `automated` plus `CATEGORY_UPDATES` as `auto-ref`. Rule `else`.

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
15. On that same account, `mailcrawl stats` prints 2 messages, 2 threads, 2 senders, month `2023-11` with count 2, `needs_review_rule0` 1, and `needs_review_else` 0. `mailcrawl dry-run` prints state `needs_review_rule0` 1 and `reply` 1, rule counts `0` 1 and `5` 1, label `STARRED` once, sender ids `1` and `2` once each, and month `2023-11` with count 2. Those counts come from evaluating the rules, including when the `decisions` table is still empty. Neither command prints a message body or an address. A later `rules` run still leaves 2 decision rows, and dry-run does not add more. No `plans` table is created. The `rules` command still stores state `needs_review` for rule `0`. The split names are only on `stats` and `dry-run`.
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
11. Attachment parsing handles `text/plain` and `text/html` only. PDF, OCR, Office, `ics`, `attachment_fields`, and image embeddings stay behind an interface with no required implementation. An unsupported type is `unsupported`. A missing backend is `skipped`. A row with no `file_sha256` is `skipped` with reason `no_bytes`. OCR thresholds are v0 constants: fewer than 25 characters per page tries OCR, and `ocr_conf` below 60 is `low_confidence`. Those thresholds are exercised only when a backend is present. This revision adds that interface in `mailcrawl/extract.py`. The default backend does not invoke `pdftotext` or `tesseract`. `mailcrawl.eml` uses `pdftotext` when that binary is on PATH. It does not invoke `tesseract`. CI installs poppler `pdftotext` and does not install an OCR binary. Image embeddings and `attachment_fields` are not created.
12. The default taxonomy is the Gmail categories in section 4.2. Content-type rules stay off until an override schema supplies their keys. `invoice_receipt`, `purchase_delivery`, and `correspondence` replace `finance`, `purchase`, and `brief` inside that example schema. Real label names never appear in code or tests. They stay in a local file outside Git.
13. Guards run first. A guard can only force `needs_review` or block `noise` and `resolved`. Later rules choose the other states. `STARRED` and `IMPORTANT` are Gmail system label ids. Rule `0` keys only on `STARRED`. `IMPORTANT` alone does not match rule `0`. Rule `spam-trash` is not a guard. It is the rule in section 4.3. Rules never read a `pseudo:` label.
14. These definitions are v0, tunable, and marked v0 in code. A human participant, as of `rules-v3` in sections 4.4 and 4.5, has a human origin and not an automated origin, is not an automated local part or a `mail.` domain, is not a transfer notice, and is not on the promotions tab. `List-Unsubscribe` or `Precedence: bulk` also means the sender is not human. When the origin label is missing, the sender also has no `List-Unsubscribe`, no `Precedence` of `bulk` or `list`, and no `Auto-Submitted` other than `no`. Unrecognized Account & Security means a sender with no `sender_decisions` row. The read-list is `sender_decisions.route = 'read'`. Owner addresses and own domains live in the local config in section 4.4. There is no separate domain file. `read_later_expiry_days` is 30, and expiry is tested on fixtures. Open questions, concrete tasks, and closing language are short clue lists in Portuguese and English. They live in a configuration file, not in the engine. The minimum question clues are a `?` at the end of a sentence outside a quote, or a start of `can you`, `could you`, `podes`, `consegues`, or `pode`. The minimum task clues are `please`, `por favor`, `preciso que`, `need you to`, and `can you send`. The minimum closing clues are `thanks`, `obrigado`, `cumprimentos`, `best regards`, `perfect`, and `combinado`. That list is not final. The client plus-three-business-days path is unimplemented. Everyone uses plus seven days. `sender_decisions.client_sla_days` exists and is NULL by default. `sender_decisions.route` may be `imbox`, `feed`, `paper_trail`, or `read`. Priority comes from `sender_decisions.priority`. When that row is missing, priority is NULL.
15. Coverage is the fraction of threads with a current decision. `needs_review` counts as covered. The report is counts by `next_state` and by `rule_id`. A sample of 100 and an error under 2% are not a fixture gate.
16. Dry-run prints counts only: state, label, sender, month, and rule. It does not write a plan file and it does not touch Gmail. Plans are phase 4.
17. `rules_version` is `rules-v4`. It was `rules-v3` before the tab, screener, billing, and handoff changes in section 4.6, `rules-v2` before the locale phrases, tab fallbacks, and screener state in section 4.5, `rules-v1` before the owner, human, housekeeping, and reference changes in section 4.4, and `rules-v0` before the rule `0` and `spam-trash` changes in section 4.3. A change to a rule, a constant, or a list increments it. `evidence` stores ids and rule or label names only, never email text.
18. No language model runs on the crawl. The first matching rule wins. There is no Gmail write, no network, and no `gog`.

## 7. Scope
**In this revision:** spec, README, security policy, contributing guide, `make check`, CI, synthetic fixtures, schema v2, body cleaning, the attachment parser interface, rules 0 to 8, `stats`, dry-run counts, `init`, idempotent `import gog-export` for the fixture contract and for a gog backup plaintext export, a streaming `import mbox` for a Takeout mbox, FTS search over the cleaned body, and the phase 1 proof.

**Out of scope:** Embeddings, label apply, Gmail writes, running `gog`, a language model on the crawl path, a full thehive backup, cache deletion, markdown-only gog exports, and the Go port stay out. Section 4 records a 25-message thehive smoke import. A full mailbox import is not this revision.

## 8. Later phases
Body cleaning, schema v2, the attachment interface, rules 0 to 8, `stats`, and dry-run counts are in this revision. Sections 4 to 7 describe that work.

An inference stays marked as still to be decided. An open approval stays marked as still to be decided.

### Phase 2. Cleaned text, parser interface, rules, and counts
Body cleaning, the attachment interface, rules 0 to 8, `stats`, and dry-run counts are done. `rules_version` is `rules-v4`. There is no model. Dry-run prints counts only. It writes no plan file and it does not touch Gmail.

A full thehive backup is not part of phase 2. Cache deletion is not part of phase 2. A sample of 100 and an error under 2% are not a fixture gate. Coverage is the counts in section 6 point 15.

### Phase 3. Embeddings
The local baseline is EmbeddingGemma 2, stored with sqlite-vec at 256 or 768 dimensions. The classification choice of model and dimension stays open. `mailcrawl embed` is a separate step outside the crawl path. The layered classifier proposal later in this section uses that embedding as a neighbour index. The Search section uses the same model with its own prefix and table. Portuguese quality and Mac speed stay unmeasured. The model choice for classification stays the SPEC-FLAG there.

The fallback named in the plan is EmbeddingGemma 1. That fallback is an inference, and it is not benchmarked. It is still to be decided.

The benchmarks are B1 retrieval, B2 label prediction, B3 scans, B4 cost, and B5 sanity. Clusters are part of this phase. The model card confirms the Apache 2.0 license and an 8,192-token context. The model is `google/embeddinggemma-2`, released 6 Oct 2026. With the vision and audio encoders disabled, it loads text-only at 270M parameters. It runs in float32 or bfloat16, never float16. It needs sentence-transformers 6.1.0 or later. Similarity is cosine. Portuguese numbers are not published. MTEB multilingual v2 is 61.36 at 768 dimensions and 60.41 at 256 dimensions. Mac speed is not published. Portuguese quality and Mac speed wait on benchmarks B1 to B5.

The official path is sentence-transformers. Community GGUF, MLX, and ONNX conversions exist. The Ollama library lists only EmbeddingGemma 1. There is no official quantized v2 checkpoint. A quantized runtime is a separate candidate in B1 and B2.

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

`mailcrawl dry-run --advise` asks that model for threads whose rule is `else`. The flag is off by default. The call receives the thread id, the rule `else`, and the lane. It does not receive message text, addresses, or label names. It returns a suggested rule, a suggested lane, and a confidence string. `by_rule` stays the deterministic rule. The `decisions` table is not written. The export adds `suggested_rule`, `suggested_lane`, and `suggested_confidence`. Those cells are blank when the flag is off, and blank for every thread that is not `else`. The JSON object `advice` appears only when the flag is on. `advice.applied` is false. `advice.model` is `por definir`. `advice.else_threads` is the `else` count.

The shipped backend is `StubAdvisor` in `mailcrawl/advise.py`. It is a deterministic stand-in for tests and for the flag. It suggests rule `5`, lane `imbox`, and confidence `0.00`. It does not load a model, and it does not open a network connection. CI does not download a model. A caller can pass another object with the same `advise` method. That object is the only backend the dry-run calls.

SPEC-FLAG: The shipped stub's model name stays `por definir`. The layered classifier proposal below compares decision models and does not pick a winner in this revision. This revision does not load a model.

SPEC-FLAG: The confidence threshold that would make a suggestion worth reading is still to be decided. The stub uses `0.00`. Nothing in this revision treats a suggestion as accepted.

SPEC-FLAG: Whether the advisor may later read subject or body text, and whether it may suggest a reply draft, is still to be decided. This revision sends neither text nor a draft.

Any cloud language model needs approval. The default is no. That choice is still to be decided.

Acceptance is that the slice beats the rules baseline on an audit sample, and that it uses no tools. That comparison is still to be decided. The proposal below names the candidates. This phase does not load them.

The layered classifier below is a proposal. It is not implemented. `rules-v4` and `StubAdvisor` stay as specified above until a later revision.

### Proposal. Layered classifier

A rules-v4 audit of 100 threads was 29.6% wrong. Excluding `else`, 19% were wrong. Of the `else` threads, 13 of 14 were wrong. Further phrase rules have plateaued. This proposal keeps a short rule layer for obvious mail and sends everything else through a decision comparison. The default candidates run locally. Jev is a test-only hosted candidate. It is not a production backend unless a held-out candidate that uses its stored answers wins under the accepted-case gate. No code in this revision changes that path. No weights are added to the repository. No review file is added. No API key is added.

The text stored in `body_text` is not clean enough for a model. The rules-v4 audit is the evidence. Phase 1 removes a quoted line, a reply header, and a `-- ` signature, and it does not cut when unsure. That pass still leaves quoted replies and signatures in real threads. R2 is a separate tested strip of quotes and signatures, and it runs before every model, including nearest neighbour. Models read that stripped thread. They do not read `body_text_raw`, and they do not treat today's `body_text` as already stripped. Today's `mailcrawl search` stays on `body_text`. The Search section is the later design. The TypeSafe jaggedness notes for Jev 1.13 say a large irrelevant state loses precision, which is why the strip is first. Laya's checkpoint context is 1024 tokens. Its input budget is 512 or 1024 tokens. A Jev test call receives only that stripped thread. An `image_only` thread is not sent to a model just because the body is empty. OCR phase 1 below marks it and routes it to review or to sender rules. Jev's image input does not change that. The vendor documents images as dreamer-ready, which means not ready for production. They sit behind the header `TypeSafe-Danger-Zone-Images`, and their formats and limits can change between releases. The docs disagree with themselves. The Images page says jev-1.13 and jev-1.14 accept images, while the System One page and the model table list text only. This design sends no images to Jev. Sources for those Jev facts are the 16 Oct 2026 research note, which opened https://docs.typesafe.ai/models, https://docs.typesafe.ai/model-jaggedness/jev-1.13, and https://typesafe.ai/blog/jev-1-14.

The layers run in this order.

1. High-precision rules. A receipt, a calendar reply, a security notice, or housekeeping can still return a decision. Receipts stay rule `3`. Calendar replies and mail to self stay rule `housekeeping`. Security notices stay rule `4`. Every other thread falls through. The fall-through rule id stays `else`. The current `rules-v4` order remains the code until a later revision narrows it to this set.
2. Nearest neighbour. The thread is embedded locally with the classification prefix. Reviewed threads and the new thread both use `task: classification | query: ` followed by the stripped text. That task is symmetric, so both sides use the same prefix. The vector is header features plus that embedding. Search uses a different prefix and a different `vec0` table, as the Search section states. Each table records its prefix and its dimension. A truncated vector is re-normalized, and a query uses the same dimension as its table. Header features are the flags the rules already compute, such as list mail, a message from me, and the category keys on the thread. The recommended default, pending confirm, puts category keys in the vector and leaves raw label names out. The index holds two local sets. One set is reviewed decisions. The other is the Gmail labels already stored for that archive. Neighbours are not copied out of the archive.
3. Decision comparison. Local candidates see the same stripped thread and the same held-out split of the local review. The suggested lane is the lane that the chosen rule already maps to. A suggestion is accepted only when its confidence is above the calibrated threshold and outside the grey band below. Accepted-case error is the share of those accepted suggestions whose rule differs from `correct_rule`. If more than one local candidate is under 2%, this revision does not rank them further. The local candidates are these.

- Logistic regression on the embedding and the header features. It is the measured baseline. It is trained locally on the review CSVs.
- Laya multilingual, fine-tuned. The checkpoint is `convaiinnovations/laya-multilingual` from the `laya` package. Laya is an open-weight non-autoregressive decision model. The published license is Apache 2.0. The checkpoint is mmBERT-base, with 322M parameters, a 1024-token context, and 100 or more languages. Mail is Portuguese and English, so this proposal uses the multilingual checkpoint. Laya answers a typed `choice` question. The options are the rule ids. Confidence is the probability of the chosen rule after temperature calibration. Published base checkpoints score about 0.35 zero-shot. A majority baseline scores about 0.46. The base checkpoint is not used as-is. Fine-tuning and temperature recalibration run on the machine that holds the review CSVs. Those CSVs stay out of Git. Downloading the weights needs Pit's approval when a later revision implements this layer.
- SetFit, few-shot. The base is a multilingual sentence embedding, and this revision does not name that checkpoint. Fine-tuning runs locally on the review CSVs. The model file stays next to the database and out of Git.

Jev is not one of those local candidates. It is test-only. No production run calls it until a candidate that uses its stored answers wins under the accepted-case gate. Jev is the TypeSafe hosted API `POST /v1/systemone`. The vendor is TypeSafe AI. It answers typed questions. It does not generate text, and it is not a reranker. There is no fine-tune. Domain fit is the state, the instructions, and the criteria. The vendor recommends Jev probabilities as features for a downstream classical model.

**Jev question battery.** One call per stripped thread carries a fixed battery of atomic questions, and every battery question goes in that call. The vendor documents that questions run in parallel and in isolation, and that adding questions barely changes latency. The battery is versioned in config with its question ids, instructions, criteria, and option order. The example shapes use synthetic wording only. A Noul asks whether the message asks the recipient to reply. A Noul asks whether it asks for an action with a stated deadline. A Noul asks whether it is a receipt or invoice. A Noul asks whether it is bulk or marketing mail. A Choice covers category keys and includes an `other` option. A Score covers urgency with described levels. Question ids are for code only, so the full question goes in `instructions`. The answers are stored once per thread with the pinned model id, the battery version, and the raw probabilities. They are not re-asked for the same thread, battery version, and model id. Code composes the lane from those stored answers.

Jev enters the decision comparison as three candidates on the same held-out split, next to logistic regression, Laya, and SetFit. J1 is a Jev Choice over rule ids, thresholded on top probability. J2 is a deterministic combiner in code over the stored battery answers. Its weights and rules are readable, and they are tuned on the review CSVs. J3 is logistic regression on the stored battery probabilities, the header features, and the embedding. Nearest neighbour plus Jev stays as one more pair. It is not the only Jev test.

English is Jev's primary language. Other languages are handled, but not equally well, and no multilingual numbers were published, so the split must include Portuguese. The vendor's 14 Oct 2026 blog expects the largest 1.14.0 gain in multilingual support, and it publishes no numbers for that gain. That is a reason to evaluate 1.14.0 on the Portuguese split before the pin moves. Context is 64k tokens per request and 32k per question. The design does not use the long-context header. An input over the limit returns `max_tokens_exceeded` and goes to review. The config pins the model version. The pin is `jev-1.13.0`. It may move to `jev-1.14.0` only after that version is stable. On 16 Oct 2026, 1.14.0 was available under the alias `jev-preview`, and `jev-latest` still pointed at 1.13.0. The same blog says `jev-latest` moves to 1.14.0 in a week. That timing is not in the docs. The blog also says the vendor gives 60 days notice before discontinuing 1.13.0 and 30 days before discontinuing 1.14.0. Those notice periods come from the blog. The config never uses `jev-latest`. Each response's `model` field is stored with the answer. `GET /v1/models` lists aliases only, but the API still accepts the pinned id. The SDK default is `jev-latest`, so the client sets the model explicitly. The option list and its order are pinned in the same config.

These limits, from the vendor's jev-1.13 notes, bind this design. They stay labelled as 1.13. Jev reads literally, so every condition is written out. Jev is unreliable at arithmetic, counting, and date comparison. Deadlines, amounts, and dates are extracted as components and compared in code. A large irrelevant state costs accuracy, so the state is the stripped thread. Adversarial text in the state can move the answer. Criteria are explicit, and the tests include synthetic phishing and marketing cases. Instructions and criteria that contradict each other hurt accuracy. Jev does not generate text.

An external study of 299,463 calls to jev-1.13.0 found the API is not deterministic. That paper states that identical calls differ by about 0.1 nats and about 1% of top choices flip. Its Table 5 lists the base-repeat flip rate as 1.0%. Those figures were measured on jev-1.13.0. No flip rate and no consistency numbers are published for 1.14.0. The double run measures them for the pin under test. The output type stays inside the option list. The study is https://arxiv.org/html/2610.16655. The vendor's self-consistency cookbook does not state the model version. It reports a borderline case run 15 times. Jev's most frequent label repeated 90.8% of the time. With a top-probability floor of 0.60, agreement was 99.2% and 74.2% of answers were automatic. Those cookbook figures are model version not stated by vendor. The vendor also lists option order as a known failure mode, with a lean toward the first option. Choice probabilities depend on the option set, so a threshold calibrated for one list does not apply to another. The vendor's 14 Oct 2026 blog chart is an internal comparison. It shows calibration error of 1.8 on 1.13.0 and 1.6 on 1.14.0. That figure is vendor-internal. It is not an independent measurement. Thresholds calibrated on 1.13.0 must be recalibrated before the pin changes to 1.14.0. The eval runs the battery twice in the pinned order and once with Choice options reversed. It reports the flip rate and the order flip rate. Thresholds are calibrated for the pinned list, and a changed list is recalibrated before it is used. The API key comes from an environment variable. The key is not written to config and not written to Git. A test call sends only the stripped thread, and only after an explicit opt-in for that account. The call breaks the local-only default. The stored row records the backend, the pinned version, the battery version, the option list, and that the stripped thread left the machine.

**Extraction.** Extraction is a later use of the same battery call. Candidates come from code first, with regexes for amounts, addresses, and dates. Jev picks among the candidates with a Choice that includes a `not stated` option. Each date part is a separate Choice, and code builds and compares the dates. Extraction answers are stored the same way as the battery.

A case whose confidence sits in a band around the threshold goes to review. It is not an accepted decision. The band width comes from the flip rate of the double run, not from a second call at decision time. Confidence depends on the question type. Choice and Score answers carry `confidence`, which is derived from `probabilities`. A Noul has no `confidence`. Its value is P(yes). A Noul is gated by two thresholds, `no` and `yes`, and a value between them goes to review. The comparison may instead gate on top probability or on the top-to-second ratio. Each of those thresholds is set per question, because top probability depends on the option count. When the rule is uncertain but the lane is clear, the suggestion may fall back to the lane only, and the row marks it as lane-only. All thresholds come from the review CSVs and the double run, not from vendor examples.

R6 writes each thread's model decision once. A later run reads that row and does not call the model again, so the lane stays the same across runs. R4 stores battery answers for the comparison and does not write this row. Reading the row is not inference. The row records its source as `rule`, `knn`, `model`, or `human`, plus the candidate, the pinned version, and the option list when a model produced it. A review row is also stored once and is not re-asked. The write does not touch Gmail.

4. No language model in phase 1. A low-confidence thread keeps the winning local suggestion and shows that confidence, unless it falls in the grey band, in which case it goes to review. A future option may call a language model only for the low-confidence band. That call would be advisory. It would see one stripped thread, not the mailbox. It would have no tools, it would not write Gmail, and it would not run on the crawl path. It is not in phase 1.
5. OCR is three steps, and only the pilot is inference. A local archive count on 16 Oct 2026, with no message text in this spec, found 1,278 threads. 62 of them, 4.9%, have a non-trivial image or PDF and under 200 characters of text. Non-trivial means the count drops inline images under 20 KB. In the 100-thread rules-v4 review, 4 threads had an image or PDF and almost empty text. Of the 29 wrong threads, 7 had an image or PDF and 1 had that almost-empty shape. None of the 29 recorded reasons named an image, a PDF, or an attachment. 28 reasons were `code` and 1 was `map`. At most 1 error in 100 is attributable to a missing image, and that link is inferred from the shape, not from the recorded reason. Sender rules and headers may already decide many of those threads. That was not measured. Section 6 point 11 still skips PDF and OCR when no backend is present. This revision stores `image_only` and extracts PDF text with `pdftotext` when that binary is on PATH. It does not install an OCR binary and it does not write a decision from the flag.
   OCR phase 1 sets `image_only` when the thread has a non-trivial image or PDF and under 200 characters of text. The flag is stored on the thread. Search can filter on it. This revision does not write a decision from the flag. Embedded PDF text is extracted with `pdftotext`. That extract is deterministic. It is not a declared inference point. CI does not install an OCR binary.
   OCR phase 1b is the pilot. It is a declared inference point, config-gated, and off by default. It runs locally on the `image_only` threads only. The engine is Apple Vision, or PP-OCRv6 when Vision is not available. PP-OCRv6 is Apache 2.0 and runs on CPU. The pilot measures how many decisions change. It does not write Gmail.
   GLM-OCR comes only after that pilot, and only if the pilot improves the measured decisions by more than 2 points. GLM-OCR is the MIT model at about 0.9B parameters. A hosted OCR API is not a candidate. It would send image bytes off the machine. Jev's experimental image input does not replace these steps. This design sends no images to Jev.

SPEC-FLAG: EmbeddingGemma 2's local vision encoder, about 170M parameters more, can embed an `image_only` thread's images into the same space for nearest neighbour, without OCR. It would be one more declared point, off by default. It would be measured on the 62-thread pilot next to Apple Vision and `pdftotext`.

Training labels come from the local review CSV written by `--export-decisions`. The human adds `correct_rule` and `correct_lane` on that file. The file stays on the machine, in the same place as the other review exports. `*.csv` stays gitignored. The repository does not store a real review file, a real thread id, a real label name, or an address. A later training command reads that local file, fits the logistic regression baseline, fine-tunes Laya, fine-tunes SetFit, and writes those model files next to the database. It does not fine-tune Jev. Model files are gitignored with the SQLite archives. The user's label map does not need an edit. One label may still map to several categories.

The default stays local. Embeddings, logistic regression, Laya, and SetFit run on the machine that holds the archive. Their inference does not open a network connection. A weights download needs Pit's approval and happens only when a later revision implements the layer. Jev runs only in the test comparison, and only after the per-account opt-in, unless that comparison has already picked it for production. Any other cloud model still needs approval, and the default is no. JSON output stays counts. It does not print a body, a subject, an address, or a label name.

`--advise` stays off by default. `StubAdvisor` stays the backend in CI and the backend when no local model file is present. It still suggests rule `5`, lane `imbox`, and confidence `0.00`, and `advice.applied` stays false. When `--advise` is on and a winning local model file is present, that file replaces the stub for threads the high-precision rules did not decide. `advice.model` names that winner. Jev is not that backend unless a Jev candidate won the held-out comparison. Until a winner exists, `advice.model` stays `por definir`. The suggestion columns stay `suggested_rule`, `suggested_lane`, and `suggested_confidence`. `by_rule` stays the deterministic rule. This revision does not write the `decisions` table. R6 is the step that writes each thread's model decision once, including a grey-band review row, and does not call the model again. R4 does not write that table. That write still does not touch Gmail.

The local comparison uses one held-out split for logistic regression, fine-tuned Laya multilingual, and SetFit. The Jev candidates on that same split are J1, J2, J3, and nearest neighbour plus Jev. The battery runs twice in the pinned order and once with Choice options reversed. The report includes the flip rate and the order flip rate. The pick is a calibrated threshold where accepted-case error is under 2%. Grey-band cases are not accepted. The audit report is still a fresh local review of 100 threads. Wrong on that report means the suggestion differs from `correct_rule`. The report has two rates. One rate is all 100 threads. The other excludes `else`, and it also states the `else` fraction, so a change in that bucket is visible. The rules-v4 rates above are the baseline. The sample stays local. Section phase 2 already says a sample of 100 and an error under 2% are not a fixture gate. CI runs a committed synthetic audited set and fails unless accepted-case error is under 2%. A rate of exactly 2% fails. An accepted set with no rows fails closed. Grey-band rows in that set are not accepted. The local sample of 100 and the held-out split stay out of Git. The held-out gate is a separate check. CI for the revision that implements the comparison runs that split and fails unless accepted-case error is under 2%, or when the flip rate is over the cap fixed in config. This revision does not choose the cap's number. The split stays out of Git. The gate fails closed when the job does not have the split. It does not commit the split to make the job pass. Synthetic tests keep using the stub. A repository test fails when the configured model name is `jev-latest`, and when the pinned option list changes without a recorded recalibration. The 100-thread rates stay in the report.

Rollout is one step at a time. Each step is its own revision.

- R0 is this proposal. No code changes.
- R1 narrows the rule layer to receipts, calendar replies, security notices, and housekeeping. Other threads are `else`. `--advise` still uses the stub.
- R2 adds the tested quote and signature strip, and runs it before any model. OCR phase 1 adds `image_only` and `pdftotext` in the same revision family. Neither step calls a model.
- R3 adds local embeddings with the classification prefix, and nearest neighbour. Suggestions fill only when `--advise` is on.
- R4 adds the Jev battery call, opt-in. It stores each battery answer so the comparison does not re-ask that thread. It does not write the `decisions` table. It runs J1, J2, J3, and nearest neighbour plus Jev next to logistic regression, Laya, and SetFit on one held-out split. It runs twice in the pinned order and once with options reversed. It trains those local models from the local review CSV. Jev stays test-only unless one of those candidates wins. The stub remains the fallback when no local winner is present. The weights download needs Pit's approval in that revision. The CI gate in the audit section starts in this revision.
- R5 stays out of phase 1. A later option may add a language model for the low-confidence band only. It would stay advisory.
- R6 is the first step that writes a decision. It writes each model decision once, including grey-band review rows, and does not re-ask. The row records the source (`rule`, `knn`, `model`, or `human`), the candidate, the pinned version, the battery version, and the option list. A Jev row also records that the stripped thread left the machine. The write does not touch Gmail. It waits until the held-out gate passes and the owner approves the write.

SPEC-FLAG: The embedding default, pending confirm, is EmbeddingGemma 2, stored with sqlite-vec in two tables, one per task prefix. The dimension, 256 or 768, is still open. The model card confirms Apache 2.0 and an 8,192-token context. Portuguese quality still needs a check before that confirm. Weights are float32 or bfloat16, never float16. A hashing vector needs no download and keeps CI offline if that check fails. It misses paraphrases, especially in Portuguese. The weights download needs approval at implementation time.

SPEC-FLAG: The neighbour-text default, pending confirm, is category keys plus the stripped thread. The vector does not include raw label names, so a count or a suggestion does not reveal a label name. Raw names stay in the local archive. They do not enter Git.

The decision model is not chosen in this revision. The local comparison is logistic regression, fine-tuned Laya multilingual, and SetFit. Jev enters as J1, J2, J3, and nearest neighbour plus Jev. The winner is under 2% accepted-case error at a threshold calibrated for the pinned option list. Review CSVs stay out of Git. Laya and SetFit weights need Pit's approval before download.

Phase 1 has no language model. A later option may call one only for the low-confidence band, after grey-band cases have gone to review. Laya is a local decision candidate. It is not a generative language model. A generative cloud language model is not a candidate. A cloud call would send mail text off the machine.

OCR phase 1 is the `image_only` flag and `pdftotext`, as layer 5 states. The pilot is off by default. The thresholds in section 6 point 11 stay in force for a backend that is present. Fewer than 25 characters per page tries OCR, and `ocr_conf` below 60 is `low_confidence`. No OCR binary is in the repository, and CI does not download one. A cloud OCR service is not a candidate.

The accept threshold is the calibrated threshold from the comparison, and the grey band around it is sized from the double-run flip rate. This revision does not set either number. The stub's `0.00` is not that threshold. A suggestion inside the band is review, not an accepted decision.

### Later phase. Lane ordering

This phase is separate from classification. It starts only after accepted-case error is under 2%. It orders threads that are already in the reply lane or the action lane. It does not move a thread to another lane. The evaluation is the owner's weekly ordering of about 30 threads. Ordering is a composite score over the stored battery answers. The inputs are the urgency Score, the reply-requested Noul, the deadline Noul, and the header features. Code combines them with weights tuned on that weekly ordering. Ordering reads stored answers and calls no model. The 16 Oct research found no published email example.

### Search

This revision implements the keyword path. `mailcrawl search` applies the SQL filters, then `msg_fts` with tokenizer `unicode61 remove_diacritics 2`, then `msg_trigram`. Filters and both FTS tables are deterministic. Vector search and Jev stay later. The steps below that name a vector or Jev do not run. The query embedding is the declared embeddings point. Fusion in SQL is deterministic given those ranks. Jev scores search hits only when that point is on, and it is off by default.

1. SQL filters run first. They are sender, labels, dates, attachment MIME type, and `image_only`. They use ordinary indexes. They do not call a model.
2. FTS5 then matches the stripped body. The tokenizer is `unicode61 remove_diacritics 2`, with no porter stemmer, because porter is English and Portuguese has no stemmer in SQLite. `bm25` weighs subject and sender above the body. A lower `bm25` score ranks higher. The index sets a prefix index so a prefix query does not scan every token. A second FTS5 table uses the trigram tokenizer for substrings and invoice numbers. Trigram search requires diacritic removal to be off, which is why it is a separate table. Regex is opt-in. SQLite does not provide `REGEXP` until the process registers it. When the pattern contains a literal of at least 3 characters, the trigram table narrows the scan first.
3. EmbeddingGemma 2 is stored in a sqlite-vec `vec0` table with metadata columns. The model card confirms the Apache 2.0 license and an 8,192-token context. The model is `google/embeddinggemma-2`, released 6 Oct 2026. With the vision and audio encoders disabled, it loads text-only at 270M parameters. It runs in float32 or bfloat16, never float16. It needs sentence-transformers 6.1.0 or later. Similarity is cosine. Portuguese numbers are not published. MTEB multilingual v2 is 61.36 at 768 dimensions and 60.41 at 256 dimensions. Mac speed is not published. The native width is 768. Truncation to 256 dimensions is tested. A truncated vector is re-normalized, and a query uses the same dimension as its table. Search is asymmetric. Stored threads use `title: ` plus the subject, then ` | text: ` plus the stripped thread, or `title: none` when there is no subject. The query uses `task: search result | query: ` plus the query text. That vector is not the classification vector. Each thread therefore has two vectors, in two `vec0` tables. Each table records its prefix and dimension. The scan is brute force. There is no approximate index and no quantization at about 2,000 messages. The research note's arithmetic puts 2,000 vectors of 768 floats near 6 MB. Vectors are embedded once during backfill and then read back. Reading them is not a new inference.
4. Fusion is plain SQL. The default is keyword-first. Keyword hits come first, and vector hits fill in after them. An optional semantic mode uses reciprocal rank fusion, `1/(60+rank)` on each list, with a weight on each list. That fusion does not call a model. The inference is only the query embedding.
5. Jev stays off. When it is on, it scores the top 20 fused rows with one Noul per query and thread pair, asking whether this thread answers the query, and sorts them by that value. One more Noul asks whether any row answers the query. The pairs for one query go in one request where context allows. The model id is the same pin as the battery, never `jev-latest`. Jev does not drop a row that fusion returned. Results are cached by query, thread, model id, and question version. It ships only if it beats fusion on the evaluation below without hurting exact-match queries. The call sends the stripped thread only, after the same per-account opt-in, and it breaks the local-only default.
6. Each result records its source. The source is `fts`, `trigram`, `vec`, `fused`, or `jev`.

The evaluation is 30 to 50 labelled real queries. They stay out of Git. The metrics are recall at 10 and mean reciprocal rank, on the same queries, for FTS alone, vectors alone, keyword-first, fusion, and fusion plus Jev. Jev runs twice, and the eval reports how often the order changes. A tier ships only when it improves one of those metrics and does not hurt exact-match queries.

### Not yet specified
The phase plan leaves these items unspecified. They are undecided.

- Read-only MCP.
- Privacy classes.
- Go port.
- Markdown-only gog exports.
- Merging an `rfc822:` mbox row with a gog API row by `rfc822_message_id`. That merge needs approval.
- The Takeout mbox facts in section 4.1 that are not marked observed or proven on thehive, 2026-10-11, and hash equality between Takeout and gog (1,634 of 2,055 differed on thehive).

Section 6 point 4 still rejects a record with no `eml` path.
