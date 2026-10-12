# mailcrawl 📬 — Your inboxes, on the record. Locally.
Local-first, read-only archive for mail you already exported. Pulls a local export into one searchable SQLite index, with provenance, and with no network on the read path.

## Status

mailcrawl is pre-alpha. This revision is schema v2, cleaned body text, the attachment interface, rules 0 to 8, and read-only `stats` and `dry-run` counts. [The spec](docs/SPEC.md) is the behavior contract.

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

`--taxonomy` is an optional local override. With no override, mailcrawl uses the default taxonomy in [the spec](docs/SPEC.md) section 4.2. A second import of the same files does not add duplicate messages or label links. If the `.eml` bytes for an existing message id change, the stored row stays and the result counts `n_error`.

A record without an `eml` path is rejected, so a markdown-only export is not imported. `internalDate` is unix milliseconds and is stored as unix seconds. `to` and `cc` are arrays of strings. Header values from the `.eml` file win over the index when both exist. Text attachment parts are searchable. A part with no bytes is `skipped` with reason `no_bytes`. An unknown binary type is `unsupported` and is not indexed as text. Headers outside the allow-list in the spec are dropped.

The native reader matches the gogcli exporter and the synthetic fixture in `testdata/fixtures/gog-export/native/`. A real thehive export was verified on the Mac at commit `32b936c` as a 25-message smoke. A full mailbox import is not this revision.

## Import a Takeout mbox

`import mbox` reads a Google Takeout mbox one message at a time, so a multi-gigabyte file is fine. It writes through the same code as `import gog-export`. The plan is one large Takeout archive first, then incremental gog imports.

```
python3 -m mailcrawl --json import mbox <file> --alias personal [--labels <labels.json>] [--taxonomy <catalogue.json>]
```

`--alias` and `--account` mean the same thing. The key is the Gmail message id in lowercase hex, read from the Takeout headers or the `From` line. A message with no Gmail id is keyed on its `Message-ID`. A message with neither is refused and counted in `n_refused`. A later gog import of the same id keeps the row, refreshes its labels, and replaces the provisional `mbox:` label and thread ids. The Takeout format is not verified on real mail yet. [The spec](docs/SPEC.md) section 4.1 has the rules.

## Customize the taxonomy

The default taxonomy has two axes. Content type is `newsletter`, `job_alert`, `notification`, `invoice_receipt`, `purchase_delivery`, `account_security`, `contract`, and `correspondence`. Origin is `human` and `automated`.

With no override, only generic signals apply. `List-Unsubscribe`, `List-Id`, and `Precedence: bulk` mark `automated`. `CATEGORY_PROMOTIONS` marks `newsletter`. `CATEGORY_UPDATES` marks `notification`. `CATEGORY_PERSONAL` marks `correspondence`. Categories with no generic signal stay empty. `stats` and `dry-run` list them in `empty_no_override`. A `pseudo:` label is never a signal.

Put account-specific label names in one JSON file. `--taxonomy` is read first, then `MAILCRAWL_TAXONOMY`, then the config key `taxonomy`, then `~/.config/mailcrawl/taxonomy.json`. `pessoal` means `personal`. A bad file exits 2. A label the account does not have is a warning in `taxonomy_warnings`, and the command still succeeds.

```json
{
  "default": {
    "newsletter": ["Example type/Bulletin"]
  },
  "thehive": {
    "invoice_receipt": ["Example type/Receipt"]
  }
}
```

Real label names stay in that local file. The repository only has example names, in `testdata/catalogue/taxonomy.synthetic.json`.

## How agents use it

The JSON CLI is `python3 -m mailcrawl`. Search returns untrusted message text. A later phase plans a read-only MCP server. `skills/mailcrawl/SKILL.md` states the boundary.

## Security and privacy

Archives stay on the local machine. Report a vulnerability in private. The steps are in [SECURITY.md](SECURITY.md).

## Roadmap

This revision is the spec, this README, the security policy, synthetic fixtures, schema v2, body cleaning, the attachment interface, rules 0 to 8, `stats`, dry-run counts, import, FTS search, and the phase 1 proof.

Phase 1 is the Python SQLite schema, an idempotent import of a gog export, FTS5 (`unicode61 remove_diacritics 2`), a JSON CLI, and fixture tests. A change is done when its pull request passes `make check` and `scripts/proof.sh` on a clean Linux runner with no network credentials.

A Go port comes after the Python prototype settles. The archive stays local.

## License

[MIT](LICENSE).

[Built by a storyteller who builds the worlds he imagines.](https://github.com/TheAngryPit)
