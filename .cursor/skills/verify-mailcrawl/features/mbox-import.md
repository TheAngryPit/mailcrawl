# Takeout mbox import

`import mbox <file> --alias <alias>` streams a Google Takeout mbox into the same writer as `import gog-export`. The synthetic fixture is `testdata/fixtures/mbox/takeout.mbox`. `testdata/fixtures/mbox/gog-after` is a native gog export of two of its messages. Both are synthetic. The fixture reproduces the shapes observed on a real thehive Takeout on 2026-10-11, never its values: mixed CRLF and LF lines, `>From ` escaping, Portuguese system labels, and `X-Gm-*`, `X-Gmail-*`, and `X-Google-*` headers. Reconciliation by message id is proven on thehive, 2026-10-11. Hash equality between Takeout and gog is not proven, and 1,634 of 2,055 thehive messages differed (SPEC section 4.1).

## Sub-features

- `mbox-import`. Init `pessoal` and import with `--alias pessoal`. The JSON reports `account` `personal`, `n_seen` 6, `n_new` 5, `n_refused` 1, and `n_error` 0.
- `mbox-search`. `search mboxtoken` returns `18867251edfa0001`, the hex form of the decimal From-line id. `search anexotoken` finds the attachment text on `18873038f7ce4003`.
- `mbox-pseudo-labels`. `Opened` is stored as `pseudo:opened`. `Archived` and `Arquivada` are stored as `pseudo:archived`. That is three messages. `Aberto` on `From 12ab@example.com` is not stored. `rules` and `dry-run` on that account print the same JSON as an account imported from a copy without those names, and neither prints `pseudo:`.
- `mbox-takeout-shapes`. `search crlftoken` returns `18867251edfa0001`, whose body mixes LF and CRLF and has a `>From ` line. Its `raw_sha256` equals the SHA-256 of the pure-CRLF gog `m1.eml`, which lacks the `X-Gm-*`, `X-Gmail-*`, and `X-Google-*` headers and keeps `X-Get-Message-Sender-Via` and `x-gong-email-entity`. Its `body_text_raw` has no CR.
- `mbox-reimport`. A second import with `--account personal` reports `n_new` 0 and `n_dup` 5. The ordered-row-dump hash does not change.
- `mbox-then-gog`. `import gog-export --dir testdata/fixtures/mbox/gog-after` reports `n_dup` 1 and `n_reconciled` 1. The archive still has 5 messages. No `mbox:` label id is left. `1886e1a4667f4002` moves to thread `1886e1a4667f4002`. All three `pseudo:` links survive, and the gog user label `Opened` (`Label_opened`) sits beside `pseudo:opened`.
- `mbox-gog-reimport`. A second `import gog-export` of the same directory reports `n_new` 0, `n_dup` 1, and `n_reconciled` 1 again. `n_reconciled` is a state count: the kept mbox row's hash still differs from the gog copy (SPEC section 4.1).
- `mbox-not-mbox`. A text file that does not start with `From ` exits 3 with `unsupported_schema` and no traceback.

## How to get to it (user POV)

- From the repo root, init a synthetic account in an isolated home, then import the fixture file.
- Import with `python3 -m mailcrawl --json import mbox testdata/fixtures/mbox/takeout.mbox --alias pessoal`.
- Reconcile with `python3 -m mailcrawl --json import gog-export --alias personal --dir testdata/fixtures/mbox/gog-after`.
- Search with `python3 -m mailcrawl --json search mboxtoken --account personal`.

## Driving it with mailcrawl-verify

Preconditions:

- Doctor exited 0 and reported `mbox import: runnable`.
- `testdata/fixtures/mbox/takeout.mbox` exists.
- `testdata/fixtures/mbox/gog-after/gmail/acct_synthetic/labels.json` and `messages/index.jsonl` exist.

- Run `"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature mbox-import`.
- Expect exit 0. The log shows the four import results, the matching rules and dry-run JSON for both accounts, the three search hits, and `unsupported_schema` for the text file.
- When a fixture file is missing, the same command exits 2.

## Gotchas

- The drive uses a temp home under `/tmp/mailcrawl-verify-scratch/$RUN_ID` and blocks network proxies. It does not run `gog` and it does not read a live mailbox.
- The fixture From lines use `@example.com` so the proof's synthetic-address scan passes. The unit tests cover the `@xxx` shape inline.
- The drive inits a second account, `vcl`, for the copy without pseudo names. It runs rules before the gog import because the gog import moves one message to a new thread.
- The structural guard (every label read outside the writer uses `GMAIL_LABEL_SQL`) is the unit test `tests/test_pseudo_labels.py`, not this drive.
- The memory bounds are unit tests, not part of this drive. `MboxMemoryTest` in `tests/test_mbox.py` generates an mbox with a 24 MB and six 4 MB random attachments and checks that peak RSS growth stays below four times the largest message. A second test imports the same file twice into one home and checks that the reimport's RSS growth stays below a quarter of the first import's, since a stored key skips the body parse. Both read `VmHWM` from `/proc/self/status` in a fresh child process, because `ru_maxrss` survives fork and exec on Linux. A 31 MB plain-text file also bounds Python allocations with tracemalloc. `AttachmentDecodeTest` in `tests/test_extract.py` checks that the base64 fast path decodes exactly like the standard library.
- `testdata/fixtures/**` is `-text` in `.gitattributes`, so git never rewrites the CRLF bytes the hash checks rely on.
- Cleanup removes the scratch directory. Evidence stays in `$EVIDENCE_DIR`.
