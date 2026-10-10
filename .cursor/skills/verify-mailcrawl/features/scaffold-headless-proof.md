# Scaffold headless proof (SPEC section 5)

Scaffold done means `make check && scripts/proof.sh` on a clean Linux runner without network credentials. The proof checks repo hygiene, synthetic fixtures, secret files, and the Python tests that exist. It does not import mail, build FTS, or run a product CLI.

## Sub-features

- `proof-runnable` — Doctor reports runnable when `Makefile` and `scripts/proof.sh` exist.
- `proof-hygiene` — required docs, ignore rules, and the secret grep.
- `proof-fixtures` — synthetic `testdata/` fixtures are present and use `example.com`.
- `proof-tests` — `python3 -m unittest discover -s tests` passes.
- `proof-fail-closed` — `mailcrawl.gog-export.v0` returns `unsupported_schema`.

## How to get to it (user POV)

- Run the proof from the repo root on Linux with fixtures only (no live credentials).

## Driving it with mailcrawl-verify

Preconditions:

- `Makefile` and `scripts/proof.sh` exist at the repo root.

- Run `"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature scaffold-headless-proof`. Expect exit **0** after `make check && scripts/proof.sh`.
- When either file is missing, the same command exits **2**.
- Proof artifacts go under gitignored `proof/` while verification evidence stays in `$EVIDENCE_DIR`.

## Gotchas

- Section 4 of the spec (import, FTS, CLI) is not part of this proof.
- Evidence for PR quotes must live in `$EVIDENCE_DIR`, not only under gitignored `proof/`.
