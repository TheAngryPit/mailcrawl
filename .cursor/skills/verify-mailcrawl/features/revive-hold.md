# Revive hold

A harness revive stays current when reevaluate runs again and the last trusted message is older than the revive. An untrusted Date header does not open it. `imported_at` after the revive does.

## Sub-features

- `hold`. After revive, a second reevaluate reports `changed` 0.
- `untrusted-date`. A message with `date_trusted` 0, an old `internal_date`, and `imported_at` after the revive lets rules replace the revive.

## How to get to it (user POV)

- Seed synthetic thread `stale-thread` and reevaluate with `llm_review` off.
- Revive it as `forgotten` / `reply`.
- Reevaluate again and see no change.
- Insert one synthetic message whose import time is newer than the revive, then reevaluate.

## Driving it with mailcrawl-verify

Proof type: command transcript.

Preconditions:

- Doctor exited 0.
- `pyproject.toml` and `mailcrawl/rules-v0.json` exist.

- Run `"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature revive-hold`.
- Expect exit 0. The log names fixture id `stale-thread` and test name `test_an_untrusted_date_imported_after_the_revive_is_queued`.
- When the package files are missing, the same command exits 2.

## Gotchas

- The drive uses the wheel's `connect` only to insert the synthetic untrusted row. It does not read a real mailbox.
- A future `--now` on the revive is a known limit and is not this drive.
- Cleanup removes the temp home. Evidence stays in `$EVIDENCE_DIR`.
