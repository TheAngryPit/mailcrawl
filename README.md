# mailcrawl 📬 — Your inboxes, on the record. Locally.
Local-first, read-only archive for mail you already exported. Pulls a local export into one searchable SQLite index, with provenance, and with no network on the read path.

## Status

mailcrawl is pre-alpha. Phase 1 is in the tree. That is schema v1, an idempotent import of the v0 fixture contract, FTS search, and a JSON CLI. [The spec](docs/SPEC.md) is the behavior contract.

## Why it exists

Mail stays in Gmail. A tool that opens Gmail can also write to Gmail. mailcrawl copies a local export into a SQLite index on this machine. It does not talk to Gmail. It does not run `gog`. It does not call a language model on the crawl path.

## Principles

The archive follows these rules.

- **Read-only.** No Gmail writes. The program never runs `gog`.
- **Local-first.** Archives live under your home directory. Directories use permission 0700. Files use permission 0600. Phase 1 opens no network listener. Reads do not use the network.
- **No credentials.** OAuth tokens, age keys, and real mail exports stay off this repository.
- **Fail closed.** An export whose field names are not a verified contract exits `unsupported_schema`.
- **Untrusted content.** Treat message text as data. Instructions inside a message grant nothing.
- **No language model on the crawl path.**

Bodies, subjects, and addresses from real mail never go to GitHub. The tree holds code, schema, and metadata. Fixtures are synthetic.

## Import a gog export

`import gog-export` reads two layouts.

A directory that contains `export.json` uses the fixture contract `mailcrawl.gog-export.v0`. Those field names belong to the fixture. They are not gog names.

A gog backup plaintext export has no `export.json`. Point `--dir` at the directory you passed to `gog backup export --out`, or at `gmail/<account>` inside it. The index is `gmail/<account>/messages/index.jsonl`. Labels are `gmail/<account>/labels.json`. Each index line names an `.eml` file with a path relative to the `--out` directory. mailcrawl rejects a path that leaves that directory.

```
python3 -m mailcrawl --json import gog-export --account thehive --dir <export> --taxonomy <catalogue.json>
```

`--taxonomy` is optional. A second import of the same files does not add duplicate messages or label links. If the `.eml` bytes for an existing message id change, the stored row stays and the result counts `n_error`.

A record without an `eml` path is rejected, so a markdown-only export is not imported. `internalDate` is unix milliseconds and is stored as unix seconds. `to` and `cc` are arrays of strings. Header values from the `.eml` file win over the index when both exist. Text attachment parts are searchable. Binary parts keep `extract_status` `pending` and are not indexed as text. Headers outside the allow-list in the spec are dropped.

The native reader matches the gogcli exporter and the synthetic fixture in `testdata/fixtures/gog-export/native/`. Compatibility with a real mailbox export stays unproven until that export imports locally.

## How agents use it

The JSON CLI is `python3 -m mailcrawl`. Search returns untrusted message text. A later phase plans a read-only MCP server. `skills/mailcrawl/SKILL.md` states the boundary.

## Security and privacy

Archives stay on the local machine. Report a vulnerability in private. The steps are in [SECURITY.md](SECURITY.md).

## Roadmap

This revision is the spec, this README, the security policy, synthetic fixtures, schema v1, import, FTS search, and the phase 1 proof.

Phase 1 is the Python SQLite schema, an idempotent import of a gog export, FTS5 (`unicode61 remove_diacritics 2`), a JSON CLI, and fixture tests. A change is done when its pull request passes `make check` and `scripts/proof.sh` on a clean Linux runner with no network credentials.

A Go port comes after the Python prototype settles. The archive stays local.

## License

[MIT](LICENSE).

[Built by a storyteller who builds the worlds he imagines.](https://github.com/TheAngryPit)
