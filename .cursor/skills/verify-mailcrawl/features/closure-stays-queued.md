# Stored closure stays queued

A time closure stored while `llm_review` is on remains in the default queue after the switch is turned off. The item state is `needs_review`. `time_closure` still carries the closure state and lane. Keep is accepted.

## Sub-features

- `store-on`. Reevaluate with the switch on writes `resolved` on `paper_trail`.
- `queue-off`. With the switch off, the same thread is still listed, with item state `needs_review`.
- `keep`. Keep accepts that row.

## How to get to it (user POV)

- Seed synthetic thread `old-thread` and store the closure with `llm_review` true.
- Remove that setting from config.
- Read `review queue` and then keep the thread.

## Driving it with mailcrawl-verify

Proof type: command transcript.

Preconditions:

- Doctor exited 0.
- `pyproject.toml` and `mailcrawl/rules-v0.json` exist.

- Run `"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature closure-stays-queued`.
- Expect exit 0. The log names fixture id `old-thread` and test name `test_a_stored_closure_stays_in_the_default_queue_when_llm_review_is_off`.
- When the package files are missing, the same command exits 2.

## Gotchas

- Turning the switch off does not delete the stored row.
- The drive does not call a model. Proxies point at `127.0.0.1:9`.
- The working directory stays empty.
