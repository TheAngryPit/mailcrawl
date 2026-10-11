# Attachment parser

Import stores a hash only when the part has bytes. `text/plain` and `text/html` are extracted. A missing hash is `skipped` / `no_bytes`. PDF, OCR, Office, and `ics` stay behind `mailcrawl.extract` and are `skipped` when no backend is supplied. Any other type is `unsupported`. OCR thresholds 25 and 60 run only for a present backend.

## Sub-features

- `no-bytes`. The v0 metadata attachment `att-synthetic-1` is `skipped`, method `no_bytes`, with a NULL `file_sha256`.
- `text-part`. The native `note.txt` part is `ok`, hashed, and searchable as `attachmenttoken`.
- `unknown-type`. The native `blob.bin` part is `unsupported`, hashed, and `ZZBINMARKERZZ` is not in the index.
- `thresholds`. A supplied PDF backend with 24 characters and confidence 59 is `low_confidence`. The default backend leaves that PDF `skipped` and does not apply the threshold.

## How to get to it (user POV)

- Init two synthetic accounts in an isolated home.
- Import `testdata/fixtures/gog-export/v0` into `personal` and `testdata/fixtures/gog-export/native` into `thehive`.
- The threshold check calls `parse_attachment` with a backend object. It does not run `pdftotext` or `tesseract`.

## Driving it with mailcrawl-verify

Preconditions:

- Doctor exited 0.
- `testdata/fixtures/gog-export/v0/export.json` exists.
- The native fixture has `labels.json` and `messages/index.jsonl` and no `export.json`.

- Run `"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature attachment-parse`.
- Expect exit 0. The log shows `skipped`/`no_bytes`, `ok` for `note.txt`, `unsupported` for `blob.bin`, and `low_confidence` for the supplied backend.
- When either fixture is missing, the same command exits 2.

## Gotchas

- The drive uses a temp home under `/tmp/mailcrawl-verify-scratch/$RUN_ID` and blocks network proxies. It does not run `gog` and it does not require an OCR binary.
- Cleanup removes that scratch directory. Evidence stays in `$EVIDENCE_DIR`.
- Body cleaning and native gog import stay separate features. This drive does not replace them.
