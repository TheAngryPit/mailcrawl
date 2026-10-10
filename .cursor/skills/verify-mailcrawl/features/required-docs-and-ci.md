# Required docs and CI wiring

A contributor (or agent) can confirm the scaffold ships the required documentation and GitHub automation files.

## Sub-features

- `docs-core` — README, SPEC, SECURITY, CONTRIBUTING present.
- `github-governance` — CODEOWNERS and Dependabot config present.
- `check-workflow` — `.github/workflows/check.yml` present for the required `check` status.

## How to get to it (user POV)

- Clone or open the mailcrawl repository locally.
- Inspect the tree at the repository root and under `.github/`.

## Driving it with mailcrawl-verify

Preconditions:

- `mailcrawl-verify doctor` exited 0 and wrote `$EVIDENCE_DIR/spec.sha256`.

- **List required paths.** Run `"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature required-docs-and-ci`. Stdout lists `present` for each required file or reports `missing`.
- **Proof.** Exit code 0 and `$EVIDENCE_DIR/drive.log` contains `present` for all seven paths.

## Gotchas

- LICENSE is enforced by `scripts/hygiene.sh` but is not part of this feature ID. The secrets feature runs the hygiene script, which includes LICENSE.
- CI runs `make check` and `scripts/proof.sh`. This feature still only checks file presence.
