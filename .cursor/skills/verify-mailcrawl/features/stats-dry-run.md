# Stats and dry-run

`mailcrawl stats` prints archive counts. `mailcrawl dry-run` prints counts of current decisions by state, label, sender id, month, and rule. Neither command writes a plan file or touches Gmail. The output is counts only.

## Sub-features

- `archive-counts`. Two synthetic messages count as 2 messages, 2 threads, 2 senders, and month `2023-11` with count 2.
- `decision-counts`. After rules v0, dry-run counts `needs_review` and `reply`, rules `0` and `5`, label `STARRED` once, and sender ids `1` and `2`.
- `read-only`. The decision row count stays 2. No `plans` table appears.
- `no-body`. The JSON output does not contain the seeded body token or an address.

## How to get to it (user POV)

- Init account `vcl` in an isolated home and seed the same two synthetic threads the rules drive uses.
- Run `python3 -m mailcrawl --json rules --account vcl --rules testdata/rules/rules-v0.json --now 1700000000`.
- Run `python3 -m mailcrawl --json stats --account vcl`.
- Run `python3 -m mailcrawl --json dry-run --account vcl`.

## Driving it with mailcrawl-verify

Preconditions:

- Doctor exited 0.
- `testdata/rules/rules-v0.json` exists.

- Run `"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature stats-dry-run`.
- Expect exit 0. The log shows `stats-dry-run`, month `2023-11`, and no message body.
- When the rules file is missing, the same command exits 2.

## Gotchas

- Category counts and `empty_no_override` are checked by the default-taxonomy drive.
- The drive uses a temp home under `/tmp/mailcrawl-verify-scratch/$RUN_ID` and blocks network proxies. It does not run `gog` and it does not write to Gmail.
- Cleanup removes that scratch directory. Evidence stays in `$EVIDENCE_DIR`.
- Sender counts use sender ids. They do not print addresses. Native gog import and body cleaning stay separate drives.
