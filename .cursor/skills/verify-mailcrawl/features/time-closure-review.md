# Time closure review

With `[review] llm_review` true, an old owner-sent-last reply becomes `resolved` on `paper_trail` and keeps a `time_closure` mark. `review queue` returns that mark. `decide` keep accepts it.

## Sub-features

- `store`. Reevaluate writes the closure while the switch is on.
- `queue`. The item includes `time_closure.state` `resolved` and `time_closure.lane` `paper_trail`.
- `keep`. A keep line is accepted and `changed` is true.

## How to get to it (user POV)

- Install the wheel into a fresh virtualenv.
- From an empty directory, init `personal` and seed synthetic thread `old-thread`.
- Turn `llm_review` on, reevaluate, and read the queue.
- Write a keep for `old-thread`.

## Driving it with mailcrawl-verify

Proof type: command transcript.

Preconditions:

- Doctor exited 0.
- `pyproject.toml` and `mailcrawl/rules-v0.json` exist.

- Run `"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature time-closure-review`.
- Expect exit 0. The log names fixture id `old-thread` and test name `test_an_enabled_step_stores_the_closure_and_keeps_it_in_scope`.
- When the package files are missing, the same command exits 2.

## Gotchas

- The drive installs `setuptools>=77` into a scratch venv, then installs the wheel. Proxies point at `127.0.0.1:9`. It does not call a model.
- The working directory stays empty. The archive is a temp home.
- Cleanup removes the scratch virtualenv. Evidence stays in `$EVIDENCE_DIR`.
