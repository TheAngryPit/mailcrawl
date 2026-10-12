# Default taxonomy

The default taxonomy is Gmail's own categories. System labels and `CATEGORY_PERSONAL`, `CATEGORY_SOCIAL`, `CATEGORY_PROMOTIONS`, `CATEGORY_UPDATES`, and `CATEGORY_FORUMS` store their id as the category key. No other content type is invented. `--taxonomy` is one local JSON override. It maps label names, and that map wins over a stored `catalogue_key` while `dry-run` counts. It can map a custom label onto a Gmail category, or it can supply another schema. `dry-run` lists content-type rules that cannot fire. `override_matches` counts messages and threads per category and does not list label names.

## Sub-features

- `gmail-categories`. With no override, `CATEGORY_PROMOTIONS`, `CATEGORY_UPDATES`, `CATEGORY_PERSONAL`, and `INBOX` keep those ids. A `pseudo:` label is not a category. `newsletter` and `automated` are absent.
- `inactive-rules`. `dry-run` sets `inactive_reason` to `gmail_default` and lists rules `2`, `2-read`, `3`, and `4`.
- `custom-label`. An override can map an example label name onto `CATEGORY_PROMOTIONS`. Content-type rules stay inactive.
- `example-schema`. `testdata/catalogue/taxonomy.synthetic.json` is an opt-in schema. Its keys include `newsletter` and `invoice_receipt`. `inactive_rules` is empty when that file is selected.
- `lookup`. The flag wins, then `MAILCRAWL_TAXONOMY`, then the config key `taxonomy`, then `~/.config/mailcrawl/taxonomy.json`.
- `validation`. Invalid JSON, a non-object, an unknown account, or a non-list of label names exits 2. A category name that is not a Gmail id is the caller's schema and exits 0.
- `missing-label`. A label name the account does not have increments `taxonomy_warnings` and is listed in `missing_labels`. The command exits 0.

## How to get to it (user POV)

- Init an account in an isolated home.
- Import a synthetic export with no `--taxonomy` and read `stats` and `dry-run`.
- Import with an override that maps `Example/Shared` onto a Gmail category.
- Import with an override that uses `newsletter` and `invoice_receipt`.
- Point `--taxonomy`, `MAILCRAWL_TAXONOMY`, the config key, and the home file at different overrides and import once at each layer.
- Pass a bad override and expect exit 2.
- Pass an override that names `Example type/Missing` and expect a warning.

## Driving it with mailcrawl-verify

Preconditions:

- Doctor exited 0.
- `testdata/catalogue/taxonomy.synthetic.json` exists.

- Run `"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature default-taxonomy`.
- Expect exit 0. The log shows `default-taxonomy`, `gmail_default`, and no message body.
- When the synthetic taxonomy file is missing, the same command exits 2.

## Gotchas

- The drive uses a temp home under `/tmp/mailcrawl-verify-scratch/$RUN_ID` and blocks network proxies. It does not run `gog` and it does not write to Gmail.
- Cleanup removes that scratch directory. Evidence stays in `$EVIDENCE_DIR`.
- Example label names in the drive are synthetic. Real label names stay out of the repository.
- `rules_version` is `rules-v2`. Content-type rules run only when the selected schema has their keys.
