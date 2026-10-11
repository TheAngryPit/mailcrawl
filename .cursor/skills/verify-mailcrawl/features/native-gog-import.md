# Native gog import

`import gog-export` reads a plaintext gog export that has no `export.json`. The synthetic fixture is `testdata/fixtures/gog-export/native`. It has one account directory, `gmail/acct_synthetic`, with `labels.json` and `messages/index.jsonl`.

## Sub-features

- `native-layout`. The fixture has labels and an index, and it has no `export.json`.
- `native-import`. Init account `thehive` and import the fixture. The JSON reports `n_seen` 3, `n_new` 3, and `n_error` 0.
- `native-search`. `search reuniao` on `thehive` returns `msg-plain`, and the snippet contains `reunião`.
- `native-reimport`. A second import reports `n_dup` 3 and `n_new` 0.
- `native-missing-dir`. A missing directory exits 3 with `unsupported_schema` and no traceback.

## How to get to it (user POV)

- From the repo root, init a synthetic account in an isolated home, then import the native fixture directory.
- Import with `python3 -m mailcrawl --json import gog-export --account thehive --dir testdata/fixtures/gog-export/native`.
- Search with `python3 -m mailcrawl --json search reuniao --account thehive`.

## Driving it with mailcrawl-verify

Preconditions:

- Doctor exited 0.
- `testdata/fixtures/gog-export/native/gmail/acct_synthetic/labels.json` and `messages/index.jsonl` exist.
- That fixture has no `export.json`.
- `testdata/catalogue/taxonomy.synthetic.json` exists.

- Run `"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature native-gog-import`.
- Expect exit 0. The log shows import counts 3, a search hit for `msg-plain`, a duplicate reimport, and `unsupported_schema` for a missing directory.
- When the fixture is missing or `export.json` is present, the same command exits 2.

## Gotchas

- The drive uses a temp home under `/tmp/mailcrawl-verify-scratch/$RUN_ID` and blocks network proxies. It does not run `gog` and it does not read a live mailbox.
- Cleanup removes that scratch directory. Evidence stays in `$EVIDENCE_DIR`.
- This feature is the native layout. The v0 `export.json` fixture stays in the scaffold proof.
