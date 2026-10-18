# Public readiness

Audited `main` at `217995a71f23cee7b8356b5a2927a298bf417169` on 18 Oct 2026. The question is what stops a stranger from installing the general program and using it from the README. This pass did not change code. It did not use the owner's mailbox or the owner's config.

A stranger can import the synthetic gog fixture after two steps the README never shows. `dry-run`, as printed in the README, exits 2.

## Ranked work

### Must-fix

1. **S.** Put `init` and `--rules testdata/rules/rules-v0.json` on the README path. `import`, `stats`, `backfill`, and `dry-run` all exit 2 until an account exists. `dry-run` then exits 2 again with `rules needs --rules or a rules path in config`. The spec names the bundled file. The README command does not pass it.
2. **S.** Tell the reader how the program is found. From the checkout, `python3 -m mailcrawl` works. From any other directory it fails with `No module named mailcrawl` until `python3 -m pip install .`. The README shows the module command and never says which of those two is required. `python3 -m venv` fails on this image because `python3-venv` is not installed. That package is not in the docs.
3. **M.** Ship a rules file with the install. `pip install .` packages `mailcrawl` only. `testdata/rules/rules-v0.json` stays in the git tree. A wheel user has no rules file to pass.
4. **S.** Replace the status line. The README says this revision is schema v2. `mailcrawl.schema.USER_VERSION` is 11. The same section omits `embed`, `knn`, `review`, and `jev`, which `python3 -m mailcrawl --help` already lists.
5. **S.** Make the license metadata match the license file. `LICENSE` is MIT. `pyproject.toml` has no `license` field. Python sources have no copyright header. Third-party texts are not in the tree. See the license section.

### Should-fix

1. **S.** Name the runtime floor and the CI packages in the README. `requires-python` is `>=3.11.10`. CI installs `poppler-utils` and `pillow`. Neither appears in the README.
2. **M.** Add a starter config a stranger can copy. `testdata/config/owners.synthetic.toml` has accounts and no `[rules] path`. There is no wizard command. Owner addresses, `security_senders`, and `test_recipients` stay empty until that file exists.
3. **S.** Add a test for the permission claim. The README says directories are mode `0700` and files are mode `0600`. No test asserts that.
4. **M.** Decide what history scan belongs in CI. `scripts/hygiene.sh` scans the current tree for token shapes. It does not scan older commits. The history scan below found no token and no mailbox. It did find fixture domains other than `example.com`, and it found public commit identities.

### Later

1. **L.** Add a clean-install job. CI checks out the repo and runs `make check` and `scripts/proof.sh`. It does not create a venv and install the wheel.
2. **L.** Add a synthetic multilingual mbox generator and a review drive against loopback. The current fixtures are enough for unit tests. They are not a demo corpus.
3. **L.** Copy model-card licenses into the tree when a weights download is approved. EmbeddingGemma and Laya are named in `docs/SPEC.md` as Apache 2.0. The `mlx` extra has no license note. No weights are in the repository.
4. **L.** Extend the existing `verify-mailcrawl` map. Do not start a second skill. The fit is described in the end-to-end section.

## 1. Clean install

There is no fresh container in this environment. Docker and Podman are absent. The transcript is a new temp directory filled with `git archive origin/main`, plus the system Python already on the image. That Python is 3.12.3. The image already had `git` and `pip`.

Commands follow the README until the README runs out. Later commands are marked as extra. They are the knowledge the docs do not give.

```
$ python3 -m mailcrawl --help          # inside the archive directory
exit=0

$ cd /tmp && python3 -m mailcrawl --help
/usr/bin/python3: No module named mailcrawl
exit=1

$ python3 -m venv /tmp/mc-venv
The virtual environment was not created successfully because ensurepip is not available.
exit=1

$ python3 -m pip install --user .      # not in the README
Successfully installed mailcrawl-0.0.0
exit=0

$ python3 -m mailcrawl --json import gog-export --account thehive --dir /tmp/does-not-exist
{"detail": "account is not initialized", "error": "usage", "ok": false}
exit=2

$ python3 -m mailcrawl --json import mbox /tmp/no-such.mbox --alias personal
{"detail": "account is not initialized", "error": "usage", "ok": false}
exit=2

$ python3 -m mailcrawl --json stats --account thehive
{"detail": "account is not initialized", "error": "usage", "ok": false}
exit=2

$ python3 -m mailcrawl --json backfill --account thehive
{"detail": "account is not initialized", "error": "usage", "ok": false}
exit=2

# extra. init is a real command and is absent from the README
$ python3 -m mailcrawl --home "$HOME" --json init --account thehive --email ada@example.com
{"account": "thehive", "ok": true}
exit=0

# extra. the native fixture path is named in the README
$ python3 -m mailcrawl --home "$HOME" --json import gog-export --account thehive --dir testdata/fixtures/gog-export/native
{"n_new": 3, "n_seen": 3, "ok": true}
exit=0

$ python3 -m mailcrawl --home "$HOME" --json stats --account thehive
{"messages": 3, "ok": true, "threads": 3}
exit=0

# the README prints this shape, with no --rules
$ python3 -m mailcrawl --home "$HOME" --json dry-run --account thehive
{"detail": "rules needs --rules or a rules path in config", "error": "usage", "ok": false}
exit=2

# extra. the spec names this file
$ python3 -m mailcrawl --home "$HOME" --json dry-run --account thehive --rules testdata/rules/rules-v0.json
{"inactive_reason": "gmail_default", "ok": true}
exit=0
```

`HOME` was an empty temp directory. The import counted 3 new messages. That is the synthetic native fixture, not a mailbox.

Steps that need knowledge outside the README:

- Run from the checkout, or `pip install` the tree first.
- On Debian, `python3-venv` is required before `venv`, and the README never mentions a venv.
- Run `init` with `--account` and `--email` before every archive command.
- Pass `--rules testdata/rules/rules-v0.json`, or write `[rules] path` in a config file.
- Pass `--home` when the archive must not land in `~/.local/share/mailcrawl`. The README mentions the default and does not show the flag. `--help` does.

## 2. License

| Path | SHA-256 | What it says |
| --- | --- | --- |
| `LICENSE` | `be9746bfaf63e9397ed0b8906c9f59247e0284cc01df5b49864d4079c8dd7e24` | MIT. Copyright 2026 Vitor Cepeda Lopes. |
| `pyproject.toml` | `231ddc74734d380cb1fd9b580d562e6746bf465e9f882bb552006b24d4f347d6` | No `license` field. `version` is `0.0.0`. |

The README links to that MIT file. No file under `mailcrawl/` carries a copyright header or an SPDX line. Skill trees under `.cursor/skills/` carry their own `LICENSE` files. Those are not the program license.

Third-party notes, from files in this tree:

- EmbeddingGemma. `docs/SPEC.md` says the model card for `google/embeddinggemma-2` confirms Apache 2.0. This audit did not re-download the card. No model-card file is in the tree. The dependency is the `vectors` extra, `sentence-transformers[image]>=6.1`.
- Laya. `docs/SPEC.md` says `convaiinnovations/laya-multilingual` is Apache 2.0. Laya is not a `pyproject.toml` dependency. No checkpoint is in the tree.
- MLX. The `mlx` extra pins `mlx>=0.32.3` and `mlx-vlm` at git commit `3d87e88402f307efbf68e568971aa887ee7d9ed0`. The repository does not name a license for either package and does not vendor their license files.

## 3. History scan

The scan walked every commit reachable from local and remote refs after `git fetch origin main`. That was 172 commits and 1879 blobs. The scanner hashed each blob with `git cat-file` and printed masks only.

What it looked for:

- Email addresses whose domain is not `example.com`, `example.org`, `example.net`, or a host ending in `.example`.
- Token shapes `ghp_`, `github_pat_`, `sk-`, `AKIA`, Slack `xox*`, `age1`, and a PEM private-key block.
- Paths under `/Users/` and `/home/`.
- Blob paths ending in `.mbox` or `.eml`.

Result:

- Allowed `example.com` addresses occurred 5058 times. Those are fixtures.
- Other domains in blobs were `client.com` (253 hits, all in `tests/test_time.py`), `notmixmax.com` (13, same file), `qq.com` (4, `tests/test_takeout_repair.py`), and `github.com` (2, a vendored skill script). Two more addresses used the reserved `.example` domain in tests. No local-part is printed here. These are fixture strings in test source, not a mailbox export.
- Four private-key hits were the detector line inside `scripts/hygiene.sh`. The line is the regular expression. It is not a key.
- One `/Users/<user>` hit is a placeholder sentence in `.cursor/skills/recall/SKILL.md`.
- `.eml` and `.mbox` blobs are the synthetic files under `testdata/fixtures/`. Code files whose names contain `takeout` were also matched by the path filter. They are Python, not an export.
- Commit identities are public git author and committer headers. The counts were 180, 108, 52, and 4 across four masked addresses. One is the human author of this repository. The others are automation identities. No message body, subject, or mailbox address appeared in those headers.

No secret and no personal mailbox data turned up. The audit continued.

`scripts/hygiene.sh` remains the CI gate. It greps the current index for token shapes and rejects tracked `*.eml` and `*.mbox` outside `testdata/`. A later commit can still hide a secret in an old blob that the tip no longer contains. That is the should-fix history item.

## 4. Security posture

What stays on the machine:

- The archive database, under `--home`, `MAILCRAWL_HOME`, or `~/.local/share/mailcrawl`.
- `~/.config/mailcrawl/config.toml` and any taxonomy file. Real addresses belong there. `config.toml` at the repo root is gitignored. `scripts/hygiene.sh` checks that ignore rule.
- Review CSV files. `*.csv` is gitignored. `tests/test_report.py::StatsDryRunTest.test_stats_and_dry_run_print_counts_only` checks that count commands do not print message text.

What can leave, and only when a command asks:

- `review` can POST to an HTTP chat endpoint. The default refuses any host other than `localhost`, `127.0.0.1`, and `::1`. `tests/test_review.py::ReviewHarnessTest.test_remote_url_is_refused_and_a_fake_local_model_can_decide` and `tests/test_review.py::ReviewHarnessTest.test_redirect_stores_nothing_and_skips_the_second_host` enforce that. `allow_remote` exists and is not the default.
- `jev` can call a hosted endpoint when that client is selected. `HttpJev.left_machine` is true. The off path writes nothing. `tests/test_jev.py::JevBatteryTest.test_off_stays_on_review_and_writes_nothing`, `tests/test_jev.py::JevBatteryTest.test_ask_outside_the_gate_does_not_open_http`, and `tests/test_jev.py::JevBatteryTest.test_pin_request_and_missing_key` cover the gate. Embeddings stay local. `tests/test_vectors.py::VectorCliTest.test_gemma_is_opt_in_and_db_v2_is_refused`.

Declared model points, all off unless config enables them, live in `mailcrawl.inference.DECLARED_POINTS`. They are `embeddings`, `search-embedding`, `jev-battery`, `decision-comparison`, `ocr-pilot`, `search-reranker`, and `low-confidence-llm`. The crawl import path is not in that list.

Tests that fence the points:

- `tests/test_inference_boundary.py::InferenceBoundaryTest.test_package_has_no_model_call_outside_a_point`
- `tests/test_inference_boundary.py::InferenceBoundaryTest.test_declared_points_are_off_by_default`
- `tests/test_inference_boundary.py::InferenceBoundaryTest.test_model_client_import_is_caught`
- `tests/test_inference_boundary.py::InferenceBoundaryTest.test_planted_violation_is_caught`

That scanner is an AST check for model-client imports and for calls named `predict`, `embed`, `generate`, `complete`, or `systemone`. It does not see `urllib`. The review and jev tests above are the HTTP proof.

Untrusted input:

- Message text is data. `tests/test_review.py::ReviewHarnessTest.test_queue_marks_text_untrusted_and_skips_an_image_only_gap` stores it under `untrusted`. `tests/test_review.py::TimeClosureReviewTest.test_closure_text_is_untrusted_and_drops_control_marks` and `tests/test_review.py::TimeClosureReviewTest.test_tag_instruction_is_empty_and_ordinary_text_stays` keep instructions in the text from becoming commands.
- Forged authentication headers do not count as the owner. `tests/test_owner.py::OwnerAliasTest.test_forged_authentication_results_stay_untrusted`.
- Header dates from a gog file do not become trusted time. `tests/test_time.py::TimeFactsTest.test_v7_gog_header_only_date_is_untrusted` and `tests/test_time.py::TimeFactsTest.test_v7_header_dates_stay_untrusted`.

The program does not run `gog`. Import reads a directory of files. No test asserts that `PATH` is never searched for a `gog` binary. That absence is a small hole in the proof, not a hole found in the code.

## 5. Onboarding gaps

Present:

- Default taxonomy is Gmail's categories. It is implemented and described in the README. `tests/test_taxonomy.py::DefaultTaxonomyTest.test_default_signals_and_empty_categories` covers it.
- A rules document exists at `testdata/rules/rules-v0.json` with `rules_version` `rules-v4`.
- Synthetic fixtures exist under `testdata/fixtures/`, `testdata/catalogue/taxonomy.synthetic.json`, and `testdata/config/owners.synthetic.toml`.
- `CONTRIBUTING.md` says fixtures use `example.com` and that `make check && scripts/proof.sh` is the done check.

Missing files and commands:

- No `mailcrawl wizard` or other config generator.
- No default rules path inside the installed package.
- No README section for install, Python version, `init`, or `--rules`.
- No single demo command that builds a synthetic archive and prints `stats`.
- No sample `config.toml` that includes `[rules] path`.

## 6. End-to-end and CI

`.github/workflows/check.yml` installs `poppler-utils`, `pillow`, and `sqlite-vec`, then runs `make check` twice and `scripts/proof.sh`. That is a checkout test. It is not a stranger install.

`/create-verification-skill` already fits. `.cursor/skills/verify-mailcrawl/SKILL.md` has Launch, Doctor, Drive, evidence, and cleanup. `.cursor/skills/verify-mailcrawl/features/README.md` indexes feature files, and each file uses the four headings the skill requires. The helper is `.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify`. The map covers docs, the hygiene gate, the scaffold proof, native gog import, mbox import, body cleaning, attachments, rules, stats, and the default taxonomy.

What that map does not drive:

- A venv created on a machine where `mailcrawl` is not already importable.
- A generated multilingual mbox.
- A fake Gmail API. The product reads a local export and does not call Gmail. The native fixture is the right stand-in. A fake API would test `gog`, which this repository does not run.
- `review run` against a loopback fake. Unit tests cover it. The feature map does not.

Proposed files, not added in this change:

- `.github/workflows/check.yml`, one extra job that installs the wheel in a new venv and runs `init`, import of `testdata/fixtures/gog-export/native`, and `dry-run --rules`.
- `scripts/demo.sh`, the same three commands, so the README can point at one line.
- `testdata/generators/mbox_multilingual.py`, a synthetic mbox writer for the languages the Takeout repair tests already name.
- `.cursor/skills/verify-mailcrawl/features/clean-install.md`.
- `.cursor/skills/verify-mailcrawl/features/review-loopback.md`.

A fake Gmail server is not on that list.
