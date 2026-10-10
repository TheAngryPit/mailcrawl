# mailcrawl verification map

This directory is the maintained source for verifying mailcrawl. Recipes exercise the repo, the hygiene gate, and the scaffold proof in docs/SPEC.md section 5.

## Baseline preconditions

- Run from the repository root with a clean git view of tracked files.
- Export `RUN_ID` and `EVIDENCE_DIR` per [SKILL.md](../SKILL.md) Launch.
- Run `mailcrawl-verify doctor` and require a recorded `docs/SPEC.md` hash under `$EVIDENCE_DIR/spec.sha256`.
- Do not modify `docs/SPEC.md` during a verification run.

## Driving conventions

- Use `.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify` for doctor, drive, and cleanup.
- One Drive step maps to one feature ID unless the feature file says otherwise.
- Log output is tee'd to `$EVIDENCE_DIR/doctor.log` and `$EVIDENCE_DIR/drive.log`.

## Proof and skip reporting

- Repo checks must match `scripts/hygiene.sh`, which `make check` and CI both run.
- Scaffold proof is runnable when `Makefile` and `scripts/proof.sh` exist. Doctor reports `runnable`. Do not claim import, FTS, or CLI checks.

## Feature entry contract

Each feature file uses exactly four H2 sections: `Sub-features`, `How to get to it (user POV)`, `Driving it with mailcrawl-verify`, `Gotchas`.

## Features

- [required-docs-and-ci](./required-docs-and-ci.md) — README, spec, security, contributing, CODEOWNERS, dependabot, check workflow.
- [secrets-and-tracked-forbidden](./secrets-and-tracked-forbidden.md) — hygiene gate for secrets and forbidden tracked paths.
- [spec-integrity](./spec-integrity.md) — SPEC hash unchanged across the run.
- [scaffold-headless-proof](./scaffold-headless-proof.md) — section 5 scaffold proof via `make check && scripts/proof.sh`.
