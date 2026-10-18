# Stats and dry-run

`mailcrawl stats` prints archive counts from stored category keys. `mailcrawl dry-run` evaluates the current rules and prints would-be decisions by state, rule, and category, plus `undecided`. A taxonomy file matches label names for that count. `--export-decisions` can write a local CSV of those threads. It does not read the `decisions` table to make those counts. Neither command writes a plan file, writes a decision, or touches Gmail. The output is counts only.

## Sub-features

- `archive-counts`. Two synthetic messages count as 2 messages, 2 threads, 2 senders, and month `2023-11` with count 2.
- `decision-counts`. Dry-run counts `needs_review_rule0` and `reply`, rules `0` and `5`, label `STARRED` once, and sender ids `1` and `2`, before any decision row exists. `else` is `needs_review_else`, not part of that rule 0 count.
- `old-archive`. `testdata/fixtures/old-taxonomy/pre-category.sql` has empty Gmail category keys. `backfill` fills two of them, leaves an existing key and a `pseudo:` id alone, and a second run reports `updated` 0.
- `read-only`. After rules v0 the decision row count stays 2. Dry-run before that leaves it at 0. No `plans` table appears.
- `no-body`. The JSON output does not contain the seeded body token or an address.

## How to get to it (user POV)

- Init account `vcl` in an isolated home and seed the same two synthetic threads the rules drive uses.
- Run `python3 -m mailcrawl --json stats --account vcl` and `python3 -m mailcrawl --json dry-run --account vcl --now 1700000000 --rules testdata/rules/rules-v0.json` before `rules`. The decision table stays empty.
- Load `testdata/fixtures/old-taxonomy/pre-category.sql` into a fresh account and run `python3 -m mailcrawl --json backfill --account vcl`.
- Run `python3 -m mailcrawl --json rules --account vcl --rules testdata/rules/rules-v0.json --now 1700000000`, then dry-run again with the same `--rules` flag.

## Driving it with mailcrawl-verify

Preconditions:

- Doctor exited 0.
- `testdata/rules/rules-v0.json` exists.

- Run `"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature stats-dry-run`.
- Expect exit 0. The log shows `stats-dry-run`, month `2023-11`, and no message body.
- When the rules file is missing, the same command exits 2.

## Gotchas

- Gmail category counts and `inactive_rules` are checked by the default-taxonomy drive.
- The drive uses a temp home under `/tmp/mailcrawl-verify-scratch/$RUN_ID` and blocks network proxies. It does not run `gog` and it does not write to Gmail.
- Cleanup removes that scratch directory. Evidence stays in `$EVIDENCE_DIR`.
- Sender counts use sender ids. They do not print addresses. Native gog import and body cleaning stay separate drives.
