# Mailcrawl in CrawlBar

Mailcrawl implements the external CLI/JSON contract `crawlkit.control.v1` in
Python. No Go rewrite or Crawlkit runtime dependency is required.

Install mailcrawl into an environment available to CrawlBar, then generate the
external manifest from that installed version:

```sh
mkdir -p ~/.crawlbar/apps
mailcrawl metadata --json > ~/.crawlbar/apps/mailcrawl.json
```

The checked-in `mailcrawl.json` is a fixture of that output. A test rejects drift
between it and `metadata()`. Generating the manifest is an operator action;
installation and tests do not write to CrawlBar's settings.

Select `personal`, `thehive`, or `vcl` in CrawlBar's mail-account setting. Its
placeholder selects the same account for status, doctor, refresh and search.
The commands use mailcrawl's normal configuration and data paths. A custom home
or config requires explicit argv changes in your external manifest, using the
existing `--home` and `--config` CLI options.

- **Status:** counts, storage size and local import receipts; no message bodies,
  account email addresses, local source paths or credentials.
- **Doctor:** configuration/schema checks and a bounded SQLite quick check.
  It reports incomplete checks instead of claiming integrity was proven.
- **Refresh (`sync`):** imports enabled local sources for the selected account.
  It never authenticates with Google or starts an exporter.
- **Search:** `--read-only` runs keyword search without migration or configured
  inference. It rejects incompatible schemas and active WAL/journals. Local
  search results contain private mail. CrawlBar's search view
  and any persisted query output must be treated as private data.

Status and doctor do not repair or migrate the archive. An active WAL or rollback
journal makes an immutable inspection unsafe; those commands return a maintenance
state rather than checkpointing it or returning stale counts.

The test suite executes the manifest's argv against synthetic local mail. This
proves the CLI contract, including appended search text and account selection.
It does not prove the native CrawlBar application UI or its binary discovery on
your device. Those need a separate installed-app check.
