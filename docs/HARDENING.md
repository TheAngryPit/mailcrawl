# Input and output limits

Mailcrawl imports local exports that may contain untrusted mail, filenames and metadata. Importers open files relative to a pinned export directory and reject symlink components. The same regular-file descriptor supplies the size check and bounded read. An operator can still select the top-level export directory and an explicit labels file.

The defaults in `mailcrawl/inputio.py` limit work before parsing or decoding.

| Input | Limit |
| --- | --- |
| Fixture manifest / native JSONL index | 16 / 32 MiB |
| Native labels | 2 MiB |
| Index record / physical mbox line | 1 MiB |
| Raw message / header | 64 MiB / 64 KiB |
| MIME parts / nesting depth | 256 / 32 |
| Decoded payload | 32 MiB |
| GOG default records / total import bytes | 100,000 / 512 MiB |
| Encoded image / GOG default new image storage per import | 8 / 256 MiB |
| PDF output / stderr | 4 MiB / 64 KiB |
| GOG default extraction work per import | 64 MiB |

Per-record limits apply independently of archive size. Mbox streams default to
the opened regular file's finite size rather than a 512 MiB archive cap. Optional
operator-supplied aggregate limits constrain work without changing message,
header, MIME, image or extraction-subprocess bounds. A growing or changed mbox
is refused instead of following an unbounded producer stream.

Oversized records can be refused. Exhausting an explicit aggregate budget aborts
the import and rolls back database changes. Newly created image files from that
import are removed on rollback; pre-existing image blobs remain. Diagnostic
samples and image rollback ownership are bounded independently of message
count. Index and manifest JSON remain bounded in-memory documents. These limits
do not guarantee an exact process RSS ceiling; large archives still require disk
space for SQLite and its transaction journal.

PDF extraction has a 30-second wall deadline and child CPU limits. Linux also enforces a 512 MiB address-space limit. macOS rejects the address-space limit used here, so it uses CPU, wall-time, input and output limits instead. A malicious native PDF parser can still allocate memory internally before producing output on macOS. Native parser vulnerability assessment is separate from these controls.

`mailcrawl/runtime_limits.py` limits stored-archive consumers independently, including archives created by older versions.

| Consumer | Limit |
| --- | --- |
| Thread context | 1 MiB of UTF-8 text |
| HTTP request / response | 2 MiB each |
| HTTP response body read | 30 seconds total, with remaining socket timeout |
| Model reason | 8,192 characters |
| Image read / selected image batch | 8 / 32 MiB |
| Images per batch | 128 |
| Benchmark thread candidates | 100,000 |
| Subject normalization | 65,536 characters |

HTTP connection setup retains its existing socket timeout. The body reader checks an aggregate deadline between bounded reads and applies the remaining timeout to supported HTTP sockets. Remote review requires HTTPS even after remote-host opt-in. Exact loopback hosts can still use HTTP. Redirect and environment-proxy refusal remain unchanged.

Embedding reads one bounded thread at a time and restricts attachment queries to selected thread IDs. Benchmark selection uses metadata before loading selected text or images. Image pixel and frame guards remain in place.

## Export presentation

Both decision CSV writers prefix a single quote to formula-leading text or text containing terminal control characters. This is spreadsheet-safe presentation encoding, not a change to archived identifiers. Ordinary Gmail identifiers remain unchanged. Consumers that need exact values should use JSON or the archive instead of treating the presentation CSV as a lossless identifier transport. Spreadsheet applications differ; no live spreadsheet execution is part of the synthetic tests.

Human CLI diagnostics escape C0, DEL and C1 controls. JSON retains the original values with JSON escaping. Human multiline output uses visible escaped newlines rather than allowing imported values to control terminal layout.

## Developer tools

The verifier allocates private scratch for each invocation and never executes an interpreter from an older shared run. The bundled watcher qualifies branch identities by repository and refuses cycles or ambiguous parents. Pretty output escapes remote control characters. The wizard refuses unsafe env destinations and atomically writes private files. Existing generated wizards are separate copies and require their own update.

These changes are local patches to vendored tooling. A future pstack refresh must preserve the protections or verify equivalent upstream fixes before replacing them.

## Remediation coverage

Numbers follow the completed 2026-10-19 source scan's finding order.

| Finding | Control and regression coverage |
| --- | --- |
| 1 | Both CSV encoders. `test_runtime_security.py`, `test_cli.py`. |
| 2 | Fresh private verifier scratch. `test_tool_security.py`. |
| 3, 4, 7, 14 | Shared import budgets, streamed messages and aggregate exhaustion rollback. `test_import_security.py`, existing import and mbox tests. |
| 5 | Bounded PDF input, output, stderr, child CPU and deadline. `test_import_security.py`. macOS address-space caveat above. |
| 6 | Qualified bounded watcher graph. `watch-pr/github.test.ts`. |
| 8 | Terminal-safe watcher output. `watch-pr/render.test.ts`. |
| 9 | Private atomic wizard env writes. `test_tool_security.py`. |
| 10, 18 | HTTP byte budgets, body deadline and bounded context. `test_runtime_security.py`, `test_jev.py`, `test_review.py`. |
| 11, 12 | Pinned-root, no-follow, same-descriptor reads for message and metadata paths. `test_import_security.py`. |
| 13 | Terminal-safe CLI text and errors. `test_cli.py`, `test_runtime_security.py`. |
| 15, 17, 19 | Bounded stored-image reads, SQL selection and streamed thread preprocessing. `test_runtime_security.py`, existing vector and benchmark tests. |
| 16 | HTTPS for remote review. `test_runtime_security.py`. |
| 20 | Linear prefix scan and subject limit. `test_runtime_security.py`. |
| 21 | Newly created image rollback and duplicate-label validation. `test_import_security.py`. |

Configuration tests preserve all supported TOML values and unknown sections when `init` changes an account. Configuration writes use private atomic replacement. Package metadata and installed runtime share one version. SQLite failures produce sanitized JSON errors. README and SPEC describe migrations and backfills during count operations. The installed-wheel journey supports macOS Bash 3.2.

The original source scan remains historical evidence at its scanned commit. This document maps the fixes and tests; it is not a new scan, a live-service penetration test, a model-quality evaluation or a guarantee against every denial-of-service input.
