# mailcrawl: spec v0

Status: scaffold. Phase 1 is specified and is not implemented in this revision. Repo: `TheAngryPit/mailcrawl`. License: MIT.

**Local-only.** mailcrawl archives mail that is already on disk. It does not open a network connection, does not read credentials, and does not write to Gmail. Unknown export shapes fail closed.

## 1. What it is
A local-first, read-only archive of your mail. Phase 1 is a Python prototype: a SQLite schema, an idempotent import of a gog export, FTS5, a JSON CLI, and fixture tests. This revision ships the scaffold around that contract. Import, FTS, and the CLI are not in this tree.

No servers, no cloud copy, no writes back to Gmail. The program never runs `gog`. No language model runs on the crawl path.

## 2. Principles
- **Read-only.** mailcrawl writes only its own config, cache, and archive. It never writes to Gmail.
- **Local-first.** Archives live under the user's home with private permissions (0700/0600). Phase 1 has no network listener. Search, stats, and show do not use the network.
- **No credentials in the tree.** OAuth tokens, age keys, and real exports never enter the repository or a log.
- **Honest outcomes.** Unknown or unverified export schemas fail closed (`unsupported_schema`) instead of guessing.
- **Privacy by default.** Bodies, subjects, and addresses from real mail never go to GitHub. The repository holds code, schema, and metadata. Tests use synthetic fixtures only (`example.com`, obvious fakes).
- **No language model on the crawl path.**
- **Neutral core.** No real mailbox, no real names, and no personal data in the repo.

## 3. Repo layout (scaffold, Python)
```
docs/SPEC.md
Makefile                         # make check
scripts/hygiene.sh               # tracked-file and secret gate
scripts/proof.sh                 # scaffold proof, temp HOME, no network
mailcrawl/contract.py            # fail-closed export gate only
tests/
testdata/fixtures/gog-export/    # synthetic; contract mailcrawl.gog-export.v0
testdata/catalogue/              # synthetic taxonomy stand-in
skills/mailcrawl/SKILL.md
.cursor/skills/verify-mailcrawl/
```
Phase 1 language is Python. A Go port is later, after the prototype settles. This revision does not add a Go module.

## 4. Phase 1 contract (not implemented here)
When phase 1 code lands, it is read-only Python and it does all of the following:

- Create the SQLite schema for one local archive.
- Import a gog export idempotently.
- Index text with FTS5 using the tokenizer `unicode61 remove_diacritics 2`.
- Speak JSON on the CLI.
- Test against synthetic fixtures only.

Until that code exists, `mailcrawl.contract.classify_export` returns `unsupported_schema` for every document, including a document labeled `mailcrawl.gog-export.v0`. That label is a name for the fixture. It is not a map of real gog field names.

## 5. Scaffold proof (this revision)
A scaffold change is done when, on a clean Linux runner with no network credentials and a temporary `HOME`, this exits 0:

```
make check && scripts/proof.sh
```

`scripts/proof.sh` uses a temp `HOME`, blocks proxy egress, and reads only the repository. It must show:

1. Required docs and the MIT `LICENSE` are present.
2. Synthetic fixtures under `testdata/` are present, use `example.com`, and are not gitignored.
3. Local archive paths, SQLite files, real-mail exports, models, and secret files are ignored, and none of those are tracked.
4. Python tests that exist pass.
5. The fixture contract `mailcrawl.gog-export.v0` fails closed with `unsupported_schema`.

The scaffold proof does not import mail, build an FTS index, or run a product CLI.

## 6. Open questions
These points are **OPEN**. This scaffold does not decide them.

1. **OPEN.** v1 DDL is the full plan schema, or ingest and FTS tables only.
2. **OPEN.** The account alias is `personal` or `pessoal`.
3. **OPEN.** `provenance_hash` uses a stable alias or an integer `account_id`. The version input is a semver or a git SHA.
4. **OPEN.** gog export field names are unverified. Until they are verified, the fixture contract `mailcrawl.gog-export.v0` fails closed (`unsupported_schema`). The fixture is not a guess of real gog keys.
5. **OPEN.** `body_text` quote and signature stripping happens in phase 1 or in phase 2.
6. **OPEN.** Phase 1 FTS columns include `from_hdr` and an empty `attachment_text`, or they do not.
7. **OPEN.** `init` creates `index.db` and `reads.db` now, or later.
8. **OPEN.** The real taxonomy catalogue stays off git. A testdata catalogue is synthetic. This revision includes only the synthetic file.
9. **OPEN.** Stored headers are an allow-list, or all headers.
10. **OPEN.** Population rules for `snippet`, `gmail_history_id`, and `subject_norm`.

## 7. Scope
**In this revision:** spec, README, security policy, contributing guide, `make check`, CI, synthetic fixtures, scaffold proof, fail-closed contract gate.

**Phase 1 (later):** the Python work in section 4.

**Out of scope:** a real import, FTS, the product CLI, embeddings, Gmail writes, running `gog`, a language model on the crawl path, and the Go port.
