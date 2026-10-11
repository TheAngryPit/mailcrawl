# Rules v0

`mailcrawl rules` applies rules 0 to 8 and prints counts. `rules_version` is `rules-v0`. There is no model. Evidence and the command output use ids and rule or label names, not message text. The catalogue leaves in the repo are synthetic.

## Sub-features

- `counts`. A `STARRED` thread counts as `needs_review` under rule `0`. A plain thread counts as `reply` under rule `5`. `covered` equals the thread count.
- `no-body`. The JSON output does not contain the seeded body token.
- `idempotent`. A second run leaves the decision row count unchanged.
- `native-still-separate`. This drive does not replace native gog import or body cleaning.

## How to get to it (user POV)

- Init account `vcl` in an isolated home.
- Seed two synthetic threads, one with Gmail label id `STARRED`.
- Run `python3 -m mailcrawl --json rules --account vcl --rules testdata/rules/rules-v0.json --now 1700000000`.

## Driving it with mailcrawl-verify

Preconditions:

- Doctor exited 0.
- `testdata/rules/rules-v0.json` exists.

- Run `"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature rules-v0`.
- Expect exit 0. The log shows `rules-v0`, covered 2, and no message body.
- When the rules file is missing, the same command exits 2.

## Gotchas

- The drive uses a temp home under `/tmp/mailcrawl-verify-scratch/$RUN_ID` and blocks network proxies. It does not run `gog` and it does not write to Gmail.
- Cleanup removes that scratch directory. Evidence stays in `$EVIDENCE_DIR`.
- Real label names stay out of this fixture. The stable keys live in `testdata/rules/rules-v0.json`.
