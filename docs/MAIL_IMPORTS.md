# Mail imports and taxonomy

## Import a gog export

`import gog-export` reads two layouts.

A directory that contains `export.json` uses the fixture contract `mailcrawl.gog-export.v0`. Those field names belong to the fixture. They are not gog names.

A gog backup plaintext export has no `export.json`. Point `--dir` at the directory you passed to `gog backup export --out`, or at `gmail/<account>` inside it. The index is `gmail/<account>/messages/index.jsonl`. Labels are `gmail/<account>/labels.json`. Each index line names an `.eml` file with a path relative to the `--out` directory. mailcrawl rejects a path that leaves that directory.

```
python3 -m mailcrawl --json import gog-export --account thehive --dir <export> --taxonomy <catalogue.json>
```

`--taxonomy` is an optional local override. With no override, mailcrawl uses the default taxonomy in [the spec](docs/SPEC.md) section 4.2. A second import of the same files does not add duplicate messages or label links. If the `.eml` bytes for an existing message id change, the stored row stays and the result counts `n_error`.

A record without an `eml` path is rejected, so a markdown-only export is not imported. `internalDate` is unix milliseconds and is stored as unix seconds. `to` and `cc` are arrays of strings. Header values from the `.eml` file win over the index when both exist. Text attachment parts are searchable. A part with no bytes is `skipped` with reason `no_bytes`. An unknown binary type is `unsupported` and is not indexed as text. Headers outside the allow-list in the spec are dropped.

The native reader matches the gogcli exporter and the synthetic fixture in `testdata/fixtures/gog-export/native/`. A real thehive export was verified on the Mac at commit `32b936c` as a 25-message smoke. A full mailbox import is not this revision.

## Import a Takeout mbox

`import mbox` reads a Google Takeout mbox one bounded message at a time. Total file size is separate from per-message memory and decoding limits; the default mbox path supports multi-gigabyte files. It writes through the same code as `import gog-export`. The plan is one large Takeout archive first, then incremental gog imports.

```
python3 -m mailcrawl --json import mbox <file> --alias personal [--labels <labels.json>] [--taxonomy <catalogue.json>]
```

`--alias` and `--account` mean the same thing. The key is the Gmail message id in lowercase hex, read from the Takeout headers or the `From` line. A message with no Gmail id is keyed on its `Message-ID`. A message with neither is refused and counted in `n_refused`. A later gog import of the same id keeps the row, refreshes its labels, and replaces the provisional `mbox:` label and thread ids. The Takeout format is not verified on real mail yet. [The spec](docs/SPEC.md) section 4.1 has the rules.

## Customize the taxonomy

The default taxonomy is Gmail's own categories. That is the system labels plus `CATEGORY_PERSONAL`, `CATEGORY_SOCIAL`, `CATEGORY_PROMOTIONS`, `CATEGORY_UPDATES`, and `CATEGORY_FORUMS`. Nothing else is inferred. A `pseudo:` label is never a category. Rules that need a content type stay off. `dry-run` says so with `inactive_reason` `gmail_default` and lists rules `2`, `2-read`, `3`, and `4` in `inactive_rules`.

One JSON file customizes this. `--taxonomy` is read first, then `MAILCRAWL_TAXONOMY`, then the config key `taxonomy`, then `~/.config/mailcrawl/taxonomy.json`. `pessoal` means `personal`. A bad file exits 2. A label the account does not have is a warning in `taxonomy_warnings`, and the command still succeeds.

Map a custom label onto a Gmail category:

```json
{"default": {"CATEGORY_PROMOTIONS": ["Example type/Bulletin"]}}
```

Or opt into your own schema. The keys are the schema. This example turns on the content types rules already know:

```json
{
  "default": {
    "newsletter": ["Example type/Bulletin"]
  },
  "thehive": {
    "invoice_receipt": ["Example type/Receipt"]
  }
}
```

`testdata/catalogue/taxonomy.synthetic.json` is that example schema with example label names. It is not the default. Real label names stay in the local file.

## Fill category keys on an existing archive

An archive imported before the Gmail category keys were stored still has the label ids. This fills the missing keys from those ids. It does not reimport, it does not change message rows, and it does not write to Gmail. A second run reports `updated` 0. `stats` and `dry-run` fill the same keys before they count.

```
python3 -m mailcrawl --json backfill --account thehive
python3 -m mailcrawl --json stats --account thehive
python3 -m mailcrawl --json dry-run --account thehive
```

`stats` prints message counts in `by_category` and `uncategorized`, plus `needs_review_rule0` and `needs_review_else`. `dry-run` evaluates the active rules and prints would-be decisions in `by_rule`, `by_state`, and `decisions_by_category`, plus `undecided`. A `--taxonomy` file matches stored label names. One label may map to several categories, and the thread carries that union. That match wins over a stored `catalogue_key` for the count, and `override_matches` reports messages and threads per category with no label names. `--export-decisions` writes a local CSV of thread id, rule, lane, override category, and a blank suggestion. `--advise` fills that suggestion for `else` threads only. It does not change the rule, and it is off by default. The model name is `por definir`. `--sample` and `--seed` keep every rule in that file. `*.csv` stays gitignored. The database directory is `--home` or `MAILCRAWL_HOME`, otherwise `~/.local/share/mailcrawl`. Rule `0` is only `STARRED`. `IMPORTANT` alone falls through. A `SPAM` or `TRASH` thread that reaches it matches rule `spam-trash` and state `resolved`, before reply or action. `else` stays `needs_review` on the decision row, and `dry-run` counts it as `needs_review_else` rather than folding it into rule `0`. `dry-run` does not write decisions. With only the Gmail default, `inactive_reason` stays `gmail_default`. `rules_version` is `rules-v4`. Phrase lists ship in English and Portuguese. Owner addresses, own domains, `security_senders`, and `test_recipients` live in `~/.config/mailcrawl/config.toml` under the account section. Mail from an owner counts as from me. Mail from a security sender matches rule `4`. A known tester recipient can match rule `test-send`. `testdata/config/owners.synthetic.toml` is the synthetic example. A `config.toml` at the repository root is gitignored.


## Operator-selected aggregate budgets

A default mbox import streams only the finite size of the regular file opened
at the start. It rejects growth, truncation or a changed file. Whole-archive byte,
record, saved-image and extracted-text totals have no fixed mbox cap. Per-message
MIME, attachment and parser limits still apply.

For either import command, the operator can set positive aggregate budgets with
`--max-bytes`, `--max-records`, `--max-image-bytes`, and `--max-extract-bytes`.
These values are CLI/configuration policy, never instructions read from mail.
Native GOG imports retain their bounded defaults. Configured sources accept the
same fields as `max_total_bytes`, `max_records`, `max_image_bytes` and
`max_extract_bytes` in `[sources.<id>.limits]`.

A configured sync is atomic per source: any refused/conflicting record prevents
its success checkpoint and rolls back that source import. The prior successful
receipt remains; a separate failed-attempt receipt records the failure. Repeating
a completed local export is idempotent. This is not a resumable per-record cursor
or proof that Gmail itself has been refreshed.
