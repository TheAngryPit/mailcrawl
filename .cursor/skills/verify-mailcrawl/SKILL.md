---
name: verify-mailcrawl
description: Verify mailcrawl repo health (required docs, CI wiring, secrets and forbidden paths, SPEC integrity), drive the scaffold headless proof, drive native gog import, drive Takeout mbox import, drive body cleaning, drive attachment parsing, drive rules v0, drive stats and dry-run counts, and drive the default taxonomy. Use before shipping doc or CI changes, or when proving make check, scripts/proof.sh, a gog export with no export.json, an mbox import and its gog reconciliation, quote stripping, attachment status, rule counts, read-only counts, or category signals.
---

# verify-mailcrawl

The verification surface is the **repository itself**: required documentation, GitHub check wiring, the hygiene gate, and `make check && scripts/proof.sh`.

## Product definition of done (scaffold)

On a clean Linux runner with **no network credentials**, this must exit **0**:

```bash
make check && scripts/proof.sh
```

That run writes `proof/summary.json` and `proof/*.log` (gitignored). It covers the phase 1 checks in [docs/SPEC.md](../../../docs/SPEC.md) section **5**, including import, FTS search, and the JSON CLI against synthetic fixtures.

Native gog import, a directory with `labels.json` and `messages/index.jsonl` and no `export.json`, is a separate drive. See [native-gog-import](./features/native-gog-import.md).

Takeout mbox import streams a synthetic mbox, reimports it, and reconciles it with a gog export of two of its messages. See [mbox-import](./features/mbox-import.md).

Body cleaning stores `body_text_raw`, indexes the cleaned `body_text`, and migrates to schema v2. See [body-clean](./features/body-clean.md).

Attachment parsing extracts text parts and leaves every other type behind an interface. See [attachment-parse](./features/attachment-parse.md).

Rules 0 to 8 print counts and do not print message text. See [rules-v0](./features/rules-v0.md).

`stats` and `dry-run` print counts and do not write a plan file. See [stats-dry-run](./features/stats-dry-run.md).

The default taxonomy applies generic signals and reads one local override. See [default-taxonomy](./features/default-taxonomy.md).

## Launch

Set an isolated run context (no servers, no credentials):

```bash
export REPO_ROOT="$(git rev-parse --show-toplevel)"
export RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
export EVIDENCE_DIR="${EVIDENCE_DIR:-/tmp/mailcrawl-verify-evidence/$RUN_ID}"
mkdir -p "$EVIDENCE_DIR"
cd "$REPO_ROOT"
```

Teardown is only env and state you created in this run (see Cleanup). There is nothing to keep alive between Drive steps.

## Doctor

Read-only preflight before Drive:

```bash
"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" doctor
```

Doctor must report:

- Current git root matches `REPO_ROOT`.
- `docs/SPEC.md` SHA-256 at run start (stored under `$EVIDENCE_DIR/spec.sha256`).
- **Scaffold proof:** `runnable` when `scripts/proof.sh` and `Makefile` exist.
- **mbox import:** `runnable` when `testdata/fixtures/mbox/takeout.mbox` and its `gog-after` export exist.

If Doctor exits non-zero, fix the repo or paths before Drive.

## Drive

Read the feature map index first:

`.cursor/skills/verify-mailcrawl/features/README.md`

Pick **one** feature file and follow its **Driving** section. Default to [scaffold-headless-proof](./features/scaffold-headless-proof.md) and run:

```bash
make check && scripts/proof.sh
```

Feature IDs match the map filenames without `.md`.

To drive native gog import:

```bash
"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature native-gog-import
```

To drive Takeout mbox import:

```bash
"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature mbox-import
```

To drive body cleaning:

```bash
"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature body-clean
```

To drive attachment parsing:

```bash
"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature attachment-parse
```

To drive rules:

```bash
"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature rules-v0
```

To drive stats and dry-run:

```bash
"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature stats-dry-run
```

To drive the default taxonomy:

```bash
"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature default-taxonomy
```

## Evidence

Capture proof outside gitignored `proof/` so cleanup does not erase it.

- Directory: **`$EVIDENCE_DIR`** (default under `/tmp/mailcrawl-verify-evidence/<RUN_ID>/`).
- Always keep: `doctor.log`, `drive.log`, `spec.sha256`, and any feature-specific outputs named in the feature file.
- Append a one-line summary to `$EVIDENCE_DIR/summary.txt` with feature ID, exit code, and timestamp.

Standards: run the real commands from the feature file. Do not mark a check passed without executing it.

## Cleanup

Remove only ephemeral run state **you** created. **Do not** delete `$EVIDENCE_DIR`.

```bash
"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" cleanup
```

Cleanup clears a temp workdir under `/tmp/mailcrawl-verify-scratch/$RUN_ID` if present. It does not stop processes (none are started) and does not remove evidence.

After cleanup, confirm evidence still exists:

```bash
test -f "$EVIDENCE_DIR/summary.txt" && test -f "$EVIDENCE_DIR/doctor.log"
```

## Helpers

```bash
"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" doctor
"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature scaffold-headless-proof
"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature native-gog-import
"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature mbox-import
"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature body-clean
"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature attachment-parse
"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature rules-v0
"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature stats-dry-run
"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature default-taxonomy
"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" cleanup
```

Subcommands: `doctor`, `drive --feature <id>`, `cleanup`, `docs-secret-check` (runs `scripts/hygiene.sh`).
