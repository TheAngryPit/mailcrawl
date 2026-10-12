# Default taxonomy

`mailcrawl` ships category keys on two axes. Content type is `newsletter`, `job_alert`, `notification`, `invoice_receipt`, `purchase_delivery`, `account_security`, `contract`, and `correspondence`. Origin is `human` and `automated`. With no override, only generic signals apply. `--taxonomy` is a local JSON override. `stats` and `dry-run` print `by_category` and `empty_no_override`.

## Sub-features

- `generic-signals`. `List-Unsubscribe`, `List-Id`, and `Precedence: bulk` mark `automated`. `CATEGORY_PROMOTIONS` marks `newsletter`. `CATEGORY_UPDATES` marks `notification`. `CATEGORY_PERSONAL` marks `correspondence`. A `pseudo:` label is not a signal.
- `empty-categories`. With no override, `empty_no_override` lists `account_security`, `contract`, `human`, `invoice_receipt`, `job_alert`, and `purchase_delivery`.
- `override`. One JSON file has `default` plus `thehive`, `vcl`, or `personal`. `pessoal` means `personal`. An account section replaces that category's default list.
- `lookup`. The flag wins, then `MAILCRAWL_TAXONOMY`, then the config key `taxonomy`, then `~/.config/mailcrawl/taxonomy.json`.
- `validation`. Invalid JSON, a non-object, an unknown category, an unknown account, or a non-list of label names exits 2.
- `missing-label`. A label name the account does not have increments `taxonomy_warnings` and is listed in `missing_labels`. The command exits 0.

## How to get to it (user POV)

- Init an account in an isolated home.
- Import a synthetic export with no `--taxonomy` and read `stats` and `dry-run`.
- Import again with an override file that has `default` and an account section.
- Point `--taxonomy`, `MAILCRAWL_TAXONOMY`, the config key, and the home file at different overrides and import once at each layer.
- Pass a bad override and expect exit 2.
- Pass an override that names `Example type/Missing` and expect a warning.

## Driving it with mailcrawl-verify

Preconditions:

- Doctor exited 0.
- `testdata/catalogue/taxonomy.synthetic.json` exists.

- Run `"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature default-taxonomy`.
- Expect exit 0. The log shows `default-taxonomy`, `empty_no_override`, and no message body.
- When the synthetic taxonomy file is missing, the same command exits 2.

## Gotchas

- The drive uses a temp home under `/tmp/mailcrawl-verify-scratch/$RUN_ID` and blocks network proxies. It does not run `gog` and it does not write to Gmail.
- Cleanup removes that scratch directory. Evidence stays in `$EVIDENCE_DIR`.
- Example label names in the drive are synthetic. Real label names stay out of the repository.
- `rules_version` stays `rules-v0`. The roles `finance`, `purchase`, and `brief` are now `invoice_receipt`, `purchase_delivery`, and `correspondence`.
