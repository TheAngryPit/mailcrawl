# Scaffold headless proof (SPEC section 5)

Phase 1 done means `make check && scripts/proof.sh` on a clean Linux runner without network credentials. The proof checks repo hygiene, synthetic fixtures, secret files, the Python tests, init, import counts, the reimport row-dump hash, `search reuniao`, and `unsupported_schema` for a missing required key.

## Sub-features

- `proof-runnable` — Doctor reports runnable when `Makefile` and `scripts/proof.sh` exist.
- `proof-hygiene` — required docs, ignore rules, and the secret grep.
- `proof-fixtures` — synthetic `testdata/` fixtures are present and use `example.com`.
- `proof-tests` — `python3 -m unittest discover -s tests` passes.
- `proof-fail-closed` — a v0 document missing a required key returns `unsupported_schema`.
- `proof-import-search` — import counts match the manifest, reimport keeps the row-dump hash, and `search reuniao` finds `reunião`.

## How to get to it (user POV)

- Run the proof from the repo root on Linux with fixtures only (no live credentials).

## Driving it with mailcrawl-verify

Preconditions:

- `Makefile` and `scripts/proof.sh` exist at the repo root.

- Run `"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature scaffold-headless-proof`. Expect exit **0** after `make check && scripts/proof.sh`.
- When either file is missing, the same command exits **2**.
- Proof artifacts go under gitignored `proof/` while verification evidence stays in `$EVIDENCE_DIR`.

## Gotchas

- The proof imports only the synthetic fixture under `testdata/`. It does not read a live mailbox.
- Evidence for PR quotes must live in `$EVIDENCE_DIR`, not only under gitignored `proof/`.
