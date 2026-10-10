# SPEC integrity during verification

The behavior contract in `docs/SPEC.md` must not change as a side effect of running verification.

## Sub-features

- `spec-hash-recorded` — Doctor stores SHA-256 of SPEC at run start.
- `spec-hash-stable` — Same hash after Drive and cleanup.

## How to get to it (user POV)

- Run verification from a checkout where SPEC matches the branch under test.

## Driving it with mailcrawl-verify

Preconditions:

- `$EVIDENCE_DIR/spec.sha256` exists from Doctor.

- **Assert unchanged.** Run `"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature spec-integrity`. Compares current SPEC hash to the recorded value.
- **Proof.** Exit code 0 and log line `docs/SPEC.md unchanged`.

## Gotchas

- Intentional SPEC edits require a new Doctor run to refresh the baseline hash.
- Every `drive` subcommand also re-checks SPEC unchanged before exiting 0.
