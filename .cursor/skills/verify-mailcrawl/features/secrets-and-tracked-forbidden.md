# Secrets and forbidden tracked files

The repository must not track env files, SQLite archives, real mail exports, credential JSON, or obvious secret material.

## Sub-features

- `required-docs-license` — README, SPEC, SECURITY, CONTRIBUTING, LICENSE on disk.
- `forbidden-paths` — no tracked `.env*`, database files, mail exports, or OAuth JSON outside `testdata/`.
- `secret-grep` — git grep finds no AWS, GitHub, Slack, or age key patterns in tracked content.
- `fixture-exceptions` — synthetic files under `testdata/` are not gitignored.

## How to get to it (user POV)

- Work in a local clone with no extra tracked secrets added for the run.

## Driving it with mailcrawl-verify

Preconditions:

- Doctor completed successfully.

- **Run the hygiene gate.** Run `"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature secrets-and-tracked-forbidden`. That runs `scripts/hygiene.sh`, the same gate as `make check` and CI.
- **Proof.** Exit code 0, stdout ends with `docs and secret check passed`, log saved to `$EVIDENCE_DIR/drive.log`.

## Gotchas

- `git grep` exit 1 means no matches. That is success.
- Untracked local secrets do not fail this check. Only tracked paths and ignore rules matter.
- Files under `testdata/` may be `.eml` or `.json` fixtures. They must stay synthetic.
