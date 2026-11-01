# Crawler operations

## Sub-features

- Read-only metadata/status/doctor, including active WAL and incomplete integrity checks.
- Local sources with transactional success and separate failure receipts.
- Repeat imports, source selection and unchanged upstream authentication.
- Explicit producer handoff and CrawlBar external-manifest command compatibility.
- Mbox streaming above the former 512 MiB limit with bounded memory.

## How to get to it (user POV)

Install the wheel, configure a synthetic local source, then run `mailcrawl sync`,
`status` and `doctor` with `--account thehive --json`. Generate CrawlBar's external
manifest with `mailcrawl metadata --json`; see the integration README.

## Driving it with mailcrawl-verify

Run the repository doctor from the root skill first. These new feature tests are
part of `make check` rather than a new `drive --feature` alias:

```sh
python3 -m unittest discover -s tests -p 'test_control.py' -v
python3 -m unittest discover -s tests -p 'test_sources.py' -v
python3 -m unittest discover -s tests -p 'test_export_handoff.py' -v
python3 -m unittest discover -s tests -p 'test_crawlbar_contract.py' -v
python3 -m unittest discover -s tests -p 'test_large_import.py' -v
```

For whole-archive budget changes, run `scripts/prove-large-import.py` in an
isolated Linux container with no network, at most 512 MiB RAM and two CPUs.
Set `TMPDIR` to disk-backed container storage: a multi-GiB fixture cannot fit in
a memory-backed `/tmp` under that RAM limit. The generator flushes batches and
asks Linux to release their file-cache pages so fixture creation does not consume
the import memory budget.
Capture its JSON receipt under `$EVIDENCE_DIR`. The default generates more than
2 GiB of synthetic repeated messages and requires peak RSS below 256 MiB. It
removes its own generated archive and database. This is a capacity regression
proof, not a distinct-message throughput benchmark.

Also exercise the installed console command (`mailcrawl --version`, metadata,
source sync, status, doctor, search) from outside the repository as part of the
wheel journey. Use only synthetic accounts and exports.

## Gotchas

Local import time does not establish Gmail freshness. Configured sync aborts an
entire source on refused/conflicting records. Status and doctor never checkpoint
WAL, repair schema or invoke an exporter. Search output is private. The optional
producer wrapper requires POSIX process groups; the producer must remain in the
foreground and must not detach child processes. No Google flow or native CrawlBar
UI is proven by fixtures, and neither is activated by this recipe.
