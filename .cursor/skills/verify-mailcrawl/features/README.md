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
- Scaffold proof is runnable when `Makefile` and `scripts/proof.sh` exist. Doctor reports `runnable` for that proof.
- Native gog import is runnable when `testdata/fixtures/gog-export/native/gmail/acct_synthetic/` has `labels.json` and `messages/index.jsonl`, and that fixture has no `export.json`. Doctor reports `native gog import: runnable` only in that case.
- mbox import is runnable when `testdata/fixtures/mbox/takeout.mbox` and `testdata/fixtures/mbox/gog-after/gmail/acct_synthetic/` with `labels.json` and `messages/index.jsonl` exist. Doctor reports `mbox import: runnable` only in that case.
- Body cleaning is runnable when `testdata/fixtures/gog-export/v0/export.json` exists. Doctor reports `body clean: runnable` only in that case.
- Attachment parsing is runnable when that v0 fixture exists and the native fixture is runnable. Doctor reports `attachment parse: runnable` only in that case.
- Rules are runnable when `testdata/rules/rules-v0.json` exists. Doctor reports `rules v0: runnable` only in that case.
- Stats and dry-run are runnable when that rules file exists. Doctor reports `stats dry-run: runnable` only in that case.
- Default taxonomy is runnable when `testdata/catalogue/taxonomy.synthetic.json` exists. Doctor reports `default taxonomy: runnable` only in that case.

## Feature entry contract

Each feature file uses exactly four H2 sections: `Sub-features`, `How to get to it (user POV)`, `Driving it with mailcrawl-verify`, `Gotchas`.

## Features

- [required-docs-and-ci](./required-docs-and-ci.md) — README, spec, security, contributing, CODEOWNERS, dependabot, check workflow.
- [secrets-and-tracked-forbidden](./secrets-and-tracked-forbidden.md) — hygiene gate for secrets and forbidden tracked paths.
- [spec-integrity](./spec-integrity.md) — SPEC hash unchanged across the run.
- [scaffold-headless-proof](./scaffold-headless-proof.md) — section 5 scaffold proof via `make check && scripts/proof.sh`.
- [native-gog-import](./native-gog-import.md). Import a synthetic gog export that has no `export.json`.
- [mbox-import](./mbox-import.md). Stream a synthetic Takeout mbox, reimport it, keep its `pseudo:` labels out of rules, and reconcile it with a gog export of the same messages.
- [body-clean](./body-clean.md). Keep `body_text_raw`, index the cleaned `body_text`, and leave schema v2 empty of plans.
- [attachment-parse](./attachment-parse.md). Extract text parts, skip a missing hash, and leave PDF, OCR, Office, and `ics` behind an interface.
- [rules-v0](./rules-v0.md). Apply rules 0 to 8 and print counts. `rules_version` is `rules-v4`.
- [stats-dry-run](./stats-dry-run.md). Print archive counts and would-be decision counts. Backfill an old archive. No plan file and no Gmail.
- [default-taxonomy](./default-taxonomy.md). Apply generic category signals, accept one local override, and print empty categories.
