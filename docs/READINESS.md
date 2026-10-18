# Public readiness

Audited `main` at `217995a71f23cee7b8356b5a2927a298bf417169` on 18 Oct 2026. The question is what stops a stranger from installing the general program and using it from the README. This pass did not change code. It did not use the owner's mailbox or the owner's config.

A wheel install does not survive `stats`. From `/tmp`, after `pip install .` into a fresh venv, `stats` exits 1 with an unhandled `FileNotFoundError`. The missing file is `site-packages/testdata/rules/rules-v0.json`.

## Ranked work

### Must-fix

1. **M.** Stop `stats` from crashing on a wheel. `bundled_rules_path()` at `mailcrawl/rules.py` line 118 returns `Path(__file__).resolve().parents[1] / "testdata" / "rules" / "rules-v0.json"`. Inside a checkout that parent is the repo root, so the file is found. On a wheel that parent is `site-packages`, and `testdata` is not packaged. `dry_run` calls it at `mailcrawl/report.py` line 97 when `config` is `None`. `stats` calls it through `_review_split` at line 359. The measured crash is `stats`, exit 1, traceback in section 1. The CLI rejects `dry-run` with exit 2 before line 97 runs. Proof type is that command transcript, including `mailcrawl.__file__`.
2. **S.** Stop `HttpJev` from following redirects with the API key. `HttpJev.ask` calls `urllib.request.urlopen` on a `Request` that sets `Authorization: Bearer`. The default opener follows redirects and keeps that header, and it honours proxy environment variables. `review._complete` already builds `ProxyHandler({})` plus `_NoRedirect`. Copy that opener. Proof type is a test that does not exist yet. It should run a local redirecting server and assert that no request carrying the key reaches the second host. `tests/test_jev.py::JevBatteryTest.test_ask_outside_the_gate_does_not_open_http` does not cover this.
3. **S.** Move fixture addresses off real registered domains. `CONTRIBUTING.md` says addresses use `example.com`. Blobs still contain registered domains in `tests/test_time.py` and `tests/test_takeout_repair.py`. One fixture uses a numeric local-part. The local-part is not printed here. Proof type is the domain census in section 3.
4. **S.** Put `init` and `--rules` on the README path. After `init`, `dry-run` with no rules file exits 2 and reports `rules needs --rules or a rules path in config`. The spec names `testdata/rules/rules-v0.json`. The README command does not pass it. Passing that path from the git tree makes `dry-run` exit 0. The wheel itself does not contain the file. Proof type is the same transcript.
5. **S.** Tell the reader how the program is found. A fresh venv whose working directory is `/tmp` raises `ModuleNotFoundError: No module named 'mailcrawl'` until `pip install .`. The README shows `python3 -m mailcrawl` and never says to install. Proof type is the transcript. An earlier `python3 -m venv` failure on this machine was the image. `python3.12-venv` was absent. After that package was installed, `venv` succeeded. That failure is not a gap in the README.
6. **S.** Replace the status line. The README says this revision is schema v2. `mailcrawl.schema.USER_VERSION` is 11. The same section omits `embed`, `knn`, `review`, and `jev`, which `python3 -m mailcrawl --help` already lists. Proof type is the README line against `USER_VERSION`.
7. **S.** Make the license metadata match the license file. `LICENSE` is MIT. `pyproject.toml` has no `license` field. Python sources have no copyright header. Third-party texts are not in the tree. See the license section. Proof type is the path and SHA table.

### Should-fix

1. **S.** Name the runtime floor and the CI packages in the README. `requires-python` is `>=3.11.10`. CI installs `poppler-utils` and `pillow`. Neither appears in the README.
2. **M.** Add a starter config a stranger can copy. `testdata/config/owners.synthetic.toml` has accounts and no `[rules] path`. There is no wizard command. Owner addresses, `security_senders`, and `test_recipients` stay empty until that file exists.
3. **S.** Add a test for the permission claim. The README says directories are mode `0700` and files are mode `0600`. No test asserts that.
4. **M.** Decide what history scan belongs in CI. `scripts/hygiene.sh` scans the current tree for token shapes. It does not scan older commits. The history scan below found no token and no mailbox. Fixture domains other than `example.com` are must-fix 3. Public commit identities remain.
5. **M.** Teach the AST fence about network and process imports. `scan_source("import urllib.request\nimport http.client\nimport socket\nimport subprocess\n", "x.py")` returns `[]` on the wheel. `MODEL_CLIENTS` lists model libraries, not those four modules. Allow them only in `review.py`, `jev.py`, `extract.py`, `bench.py`, and `version.py`. Those are the modules that import `urllib` or `subprocess` today. `urllib` in `jev.py` is inside `HttpJev.ask`, so a top-of-file search misses it. Proof type is that `scan_source` result plus the import sites.

### Later

1. **L.** Add a clean-install job. CI checks out the repo and runs `make check` and `scripts/proof.sh`. It does not create a venv and install the wheel.
2. **L.** Add a synthetic multilingual mbox generator and a review drive against loopback. The current fixtures are enough for unit tests. They are not a demo corpus.
3. **L.** Copy model-card licenses into the tree when a weights download is approved. EmbeddingGemma and Laya are named in `docs/SPEC.md` as Apache 2.0. The `mlx` extra has no license note. No weights are in the repository.
4. **L.** Extend the existing `verify-mailcrawl` map. Do not start a second skill. The fit is described in the end-to-end section.

## 1. Clean install

There is no fresh container in this environment. Docker and Podman are absent. Python is 3.12.3. The first attempt to run `python3 -m venv` failed because this image had no `python3.12-venv`. That failure is specific to the image. After `apt install python3.12-venv`, the venv below succeeded.

The run that counts is a new venv, `pip install` of the checkout, and a working directory of `/tmp`. `mailcrawl.__file__` is under `site-packages`, so this is the wheel, not the source tree.

```
$ python3 -m venv /tmp/mc-empty-venv
$ cd /tmp && /tmp/mc-empty-venv/bin/python -c 'import mailcrawl'
ModuleNotFoundError: No module named 'mailcrawl'
exit=1

$ python3 -m venv /tmp/mc-wheel-venv
$ /tmp/mc-wheel-venv/bin/python -m pip install /workspace
Successfully installed mailcrawl-0.0.0
exit=0

$ cd /tmp
$ /tmp/mc-wheel-venv/bin/python -c 'import mailcrawl; print(mailcrawl.__file__)'
/tmp/mc-wheel-venv/lib/python3.12/site-packages/mailcrawl/__init__.py

$ /tmp/mc-wheel-venv/bin/python -m mailcrawl --home /tmp/tmp.AMEO1hidiJ --json init --account thehive --email ada@example.com
{"account": "thehive", "database": "/tmp/tmp.AMEO1hidiJ/thehive.db", "ok": true}
exit=0

$ /tmp/mc-wheel-venv/bin/python -m mailcrawl --home /tmp/tmp.AMEO1hidiJ --json import gog-export --account thehive --dir /tmp/does-not-exist
{"detail": "export directory is missing", "error": "unsupported_schema", "ok": false}
exit=3

$ /tmp/mc-wheel-venv/bin/python -m mailcrawl --home /tmp/tmp.AMEO1hidiJ --json import gog-export --account thehive --dir /workspace/testdata/fixtures/gog-export/native
{"account": "thehive", "database": "/tmp/tmp.AMEO1hidiJ/thehive.db", "missing_labels": [], "n_dup": 0, "n_error": 0, "n_new": 3, "n_reconciled": 0, "n_refused": 0, "n_seen": 3, "ok": true, "takeout_unmapped": [], "taxonomy_warnings": 0}
exit=0

$ /tmp/mc-wheel-venv/bin/python -m mailcrawl --home /tmp/tmp.AMEO1hidiJ --json stats --account thehive
Traceback (most recent call last):
  File "<frozen runpy>", line 198, in _run_module_as_main
  File "<frozen runpy>", line 88, in _run_code
  File "/tmp/mc-wheel-venv/lib/python3.12/site-packages/mailcrawl/__main__.py", line 6, in <module>
    raise SystemExit(main())
  File "/tmp/mc-wheel-venv/lib/python3.12/site-packages/mailcrawl/cli.py", line 88, in main
    return _stats(args)
  File "/tmp/mc-wheel-venv/lib/python3.12/site-packages/mailcrawl/cli.py", line 482, in _stats
    _emit(args, {**_counted(args, report), "ok": True})
  File "/tmp/mc-wheel-venv/lib/python3.12/site-packages/mailcrawl/cli.py", line 777, in _counted
    counted = report(conn, active_categories(loaded, alias), names)
  File "/tmp/mc-wheel-venv/lib/python3.12/site-packages/mailcrawl/cli.py", line 472, in report
    return stats(
  File "/tmp/mc-wheel-venv/lib/python3.12/site-packages/mailcrawl/report.py", line 52, in stats
    rule0, fallthrough = _review_split(
  File "/tmp/mc-wheel-venv/lib/python3.12/site-packages/mailcrawl/report.py", line 359, in _review_split
    config = load_rules(bundled_rules_path())
  File "/tmp/mc-wheel-venv/lib/python3.12/site-packages/mailcrawl/rules.py", line 82, in load_rules
    document = json.loads(path.read_text(encoding="utf-8"))
FileNotFoundError: [Errno 2] No such file or directory: '/tmp/mc-wheel-venv/lib/python3.12/site-packages/testdata/rules/rules-v0.json'
exit=1

$ /tmp/mc-wheel-venv/bin/python -m mailcrawl --home /tmp/tmp.AMEO1hidiJ --json dry-run --account thehive
{"detail": "rules needs --rules or a rules path in config", "error": "usage", "ok": false}
exit=2

$ /tmp/mc-wheel-venv/bin/python -m mailcrawl --home /tmp/tmp.AMEO1hidiJ --json dry-run --account thehive --rules /workspace/testdata/rules/rules-v0.json
{"by_category": {"CATEGORY_FORUMS": 0, "CATEGORY_PERSONAL": 0, "CATEGORY_PROMOTIONS": 0, "CATEGORY_SOCIAL": 0, "CATEGORY_UPDATES": 0, "CHAT": 0, "DRAFT": 0, "IMPORTANT": 0, "INBOX": 2, "SENT": 0, "SPAM": 0, "STARRED": 1, "TRASH": 0, "UNREAD": 0}, "by_label": {"Example/Synthetic": 2, "INBOX": 2, "STARRED": 1}, "by_month": {"2026-04": 3}, "by_rule": {"0": 1, "5": 1, "else": 1}, "by_sender": {"1": 2, "2": 1}, "by_state": {"needs_review": 1, "needs_review_else": 1, "needs_review_rule0": 1}, "decisions_by_category": {"INBOX": {"5": 1, "else": 1}, "STARRED": {"0": 1}}, "inactive_reason": "gmail_default", "inactive_rules": ["2", "2-read", "3", "4"], "missing_labels": [], "ok": true, "taxonomy_warnings": 0, "undecided": 1}
exit=0
```

The import counted 3 new messages. That is the synthetic native fixture, not a mailbox. The successful `dry-run` read the rules file from the git checkout. The installed package does not contain it.

Steps that need knowledge outside the README:

- `pip install` the tree. Running the module from another directory does not see the checkout.
- Run `init` with `--account` and `--email` before import.
- Do not expect `stats` to work on a wheel until the rules file is packaged. The crash is unhandled.
- Pass `--rules` with a real path, or write `[rules] path` in a config file, before `dry-run`.
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

The first count was wrong about what it counted. `git rev-list --all` on the refs this clone had then was 172 commits. Every line of `git rev-list --all --objects` was treated as a blob. That was 1879 lines. Those lines are commits, trees, and blobs together.

The recount asks `git cat-file --batch-check=%(objecttype)` for each unique oid from `git rev-list --objects --all`.

- After `git fetch origin '+refs/heads/*:refs/remotes/origin/*'`, branch refs hold 173 commits, 766 trees, and 944 blobs.
- After also fetching `refs/pull/*/head` and `refs/pull/*/merge`, the same command reports 175 commits, 766 trees, and 944 blobs. The two added commits are pull-request refs. One is `refs/pull/53/merge`, the GitHub merge commit for this pull request. It is not on the branch.

An independent mirror clone reported 175 commits and 944 unique blobs. This recount matches that. That independent pass also ran gitleaks 8.30.1 over all refs and reported 0 leaks. This revision did not execute gitleaks.

The content scan below hashed blob bytes with `git cat-file` and printed masks only. It ran on the blob set before the pull-request refs were fetched. Those refs added commits and no blobs, so the blob set stayed 944.

What it looked for:

- Email addresses whose domain is not `example.com`, `example.org`, `example.net`, or a host ending in `.example`.
- Token shapes `ghp_`, `github_pat_`, `sk-`, `AKIA`, Slack `xox*`, `age1`, and a PEM private-key block.
- Paths under `/Users/` and `/home/`.
- Blob paths ending in `.mbox` or `.eml`.

Result:

- Allowed `example.com` addresses occurred 5058 times. Those are fixtures.
- Other domains in blobs were registered domains in `tests/test_time.py` (253 hits and 13 hits) and `tests/test_takeout_repair.py` (4 hits), plus `github.com` (2, a vendored skill script). Two more addresses used the reserved `.example` domain in tests. No local-part is printed here. These are fixture strings in test source, not a mailbox export.
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
- `jev` can call a hosted endpoint when that client is selected. `HttpJev.left_machine` is true. The off path writes nothing. `tests/test_jev.py::JevBatteryTest.test_off_stays_on_review_and_writes_nothing`, `tests/test_jev.py::JevBatteryTest.test_ask_outside_the_gate_does_not_open_http`, and `tests/test_jev.py::JevBatteryTest.test_pin_request_and_missing_key` cover the gate. They do not cover redirects. `HttpJev.ask` uses `urllib.request.urlopen`, so a redirect keeps the `Authorization` header and proxy environment variables apply. That is must-fix 2. Embeddings stay local. `tests/test_vectors.py::VectorCliTest.test_gemma_is_opt_in_and_db_v2_is_refused`.

Declared model points, all off unless config enables them, live in `mailcrawl.inference.DECLARED_POINTS`. They are `embeddings`, `search-embedding`, `jev-battery`, `decision-comparison`, `ocr-pilot`, `search-reranker`, and `low-confidence-llm`. The crawl import path is not in that list.

Tests that fence the points:

- `tests/test_inference_boundary.py::InferenceBoundaryTest.test_package_has_no_model_call_outside_a_point`
- `tests/test_inference_boundary.py::InferenceBoundaryTest.test_declared_points_are_off_by_default`
- `tests/test_inference_boundary.py::InferenceBoundaryTest.test_model_client_import_is_caught`
- `tests/test_inference_boundary.py::InferenceBoundaryTest.test_planted_violation_is_caught`

That scanner is an AST check for model-client imports and for calls named `predict`, `embed`, `generate`, `complete`, or `systemone`. A source string that only imports `urllib.request`, `http.client`, `socket`, and `subprocess` scans clean. Closing that hole is should-fix 5. The review tests above are the HTTP proof for the review command. They are not a proof for `HttpJev`.

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
- No rules file inside the installed package. `stats` looks for it beside the package and crashes. See section 1.
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

- `.github/workflows/check.yml`, one extra job that installs the wheel in a new venv and runs `init`, import of `testdata/fixtures/gog-export/native`, and `stats`. That job fails today. Section 1 is the failure.
- `scripts/demo.sh`, the same three commands, so the README can point at one line.
- `testdata/generators/mbox_multilingual.py`, a synthetic mbox writer for the languages the Takeout repair tests already name.
- `.cursor/skills/verify-mailcrawl/features/clean-install.md`.
- `.cursor/skills/verify-mailcrawl/features/review-loopback.md`.

A fake Gmail server is not on that list.
