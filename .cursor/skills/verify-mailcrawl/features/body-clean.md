# Body cleaning

Import stores the phase 1 text in `body_text_raw` and the cleaned text in `body_text`. FTS indexes the cleaned text. A `>` quote and a `-- ` signature drop. An HTML blockquote that is not one of those markers stays.

## Sub-features

- `raw-kept`. The v0 fixture message `fixture-001` keeps `> quoted line stays` and the `-- ` signature in `body_text_raw`.
- `cleaned-indexed`. `body_text` for that message is `This body is fake fixture text.` `search quoted` returns no hits. `search fake` returns `fixture-001`.
- `schema-v2`. The account database is `user_version` 5 and has `decisions`, `sender_decisions`, and `thread_mutes`. It has no `plans`, `proposals`, `embeddings`, or `clips`.
- `html-blockquote-stays`. A synthetic HTML blockquote that does not use a `>` line stays searchable.

## How to get to it (user POV)

- From the repo root, init a synthetic account in an isolated home, then import `testdata/fixtures/gog-export/v0`.
- Search with `python3 -m mailcrawl --json search quoted --account personal` and `python3 -m mailcrawl --json search fake --account personal`.
- The HTML check imports one extra synthetic `export.json` that exists only under the temp home.

## Driving it with mailcrawl-verify

Preconditions:

- Doctor exited 0.
- `testdata/fixtures/gog-export/v0/export.json` exists.

- Run `"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature body-clean`.
- Expect exit 0. The log shows an empty `quoted` search, a `fake` hit for `fixture-001`, schema v2, and a hit for the HTML blockquote word.
- When the v0 fixture is missing, the same command exits 2.

## Gotchas

- The drive uses a temp home under `/tmp/mailcrawl-verify-scratch/$RUN_ID` and blocks network proxies. It does not run `gog` and it does not read a live mailbox.
- Cleanup removes that scratch directory. Evidence stays in `$EVIDENCE_DIR`.
- Native gog import is a separate feature. This drive does not replace it.
