# Mailcrawl in the crawler family

Mailcrawl remains a Python product. It consumes local mail exports and owns
their normalization, identity reconciliation, SQLite schema, triage and review.
Google authentication, OAuth refresh and export production remain outside this
process. A source configuration contains local paths, never commands or tokens.

## Shared contracts

The design follows the public [Crawlkit boundary](https://github.com/openclaw/crawlkit/blob/ca93c9f70a4dad4223c3788d4fc526770ee6a8c9/README.md)
and [CrawlBar control protocol](https://github.com/openclaw/crawlbar/blob/7b69beada7f42c34323ef58d32ca713a47f6c566/docs/control-protocol.md),
reviewed on 19 October 2026. Compatibility uses JSON and command arguments;
there is no Go dependency in the Python package.

| Crawlkit responsibility | Python implementation or decision |
| --- | --- |
| `control` | `metadata`, `status` and `doctor` expose versioned capability and health data; an external CrawlBar manifest uses those commands. |
| `store` | Existing SQLite schema and migrations remain authoritative; inspection does not invoke writable migration helpers. |
| `state` | Configured source identities and local completion receipts use existing account-scoped cursors. |
| `output` | Existing terminal-control encoding and JSON remain; health output excludes message contents and config secrets. |
| `embed`, `vector` | Existing optional local inference stays separate from ingestion and off until configured. |
| `scheduler`, `worker` | No daemon or scheduler is installed. Explicit local refresh remains one finite invocation. |
| `snapshot`, `backup`, `mirror`, `remote` | Not adopted in this delivery. No automatic backups, cloud archives or publication. |

## Refresh and evidence

`[sources.<id>]` defines an account, source kind and path. Paths relative to the
config file resolve beside that file. The source ID is stable across retries.
Only configured local `gog-export` and `mbox` inputs are accepted.

`sync` always reprocesses the selected export through the safe importer. It does
not skip work based on directory mtimes or an index-only hash. Message identity
makes retries idempotent; absent records do not delete archived messages.

Successful completion receipts are written in the same SQLite transaction as
the import. A refused or conflicting record makes the configured-source refresh
fail and roll back. A separate failed-attempt receipt, when the database remains writable, preserves observability
without advancing the successful import time. Ordinary manual imports retain
their explicit refusal/conflict counts.

Failed batch results contain `attempted_counts` rather than counts of committed
new rows. `receipt_persisted=false` means the database could not record that
failed attempt; the structured command result remains authoritative for it.

Three dates have different meanings. `last_import_at` is a successful local
completion time. `last_attempt_at` can describe a failure. Message dates describe
mail content and never establish freshness. A recent local import does not prove
that the producer exported the latest Gmail state.

The manifest advertises only supported local actions. It does not turn a search
result into an instruction or permission to access Google. Source contents,
message bodies and model responses remain untrusted data.

## Export handoff

1. The separately authorized producer completes its export into a private local
   directory. Use its normal authentication and security profile.
2. The operator points the configured source at that completed directory. Do not
   refresh while another process is still replacing export files.
3. Run `mailcrawl sync --source <id> --json` and inspect the exit code and counts.
4. Read `mailcrawl status --json` to distinguish last success, failure and an old
   local completion receipt. An export failure means no import is launched.

The optional orchestration script runs an explicitly selected external producer
before importing. Use an environment where mailcrawl is installed:

```sh
python3 scripts/refresh-mailcrawl.py --source daily \
  --producer /absolute/path/to/reviewed-export-wrapper \
  --config /path/to/config.toml --home /path/to/mailcrawl-archive
```

The optional orchestration script requires POSIX (macOS/Linux) for process-group
cleanup; this wrapper is not a Windows service. Its producer must remain in the foreground and must not detach
children into another session. On exit, timeout or SIGTERM, the script terminates its
owned process group before allowing import. It revalidates the selected source
against the original configuration before the handoff; configuration drift
fails closed. SIGKILL cannot run graceful cleanup; detached descendants are
outside this wrapper contract.

The wrapper contract is `wrapper --out <configured-local-export-directory>`.
The operator supplies and reviews that wrapper; its existing tool, identity,
service selection, permissions and safety profile remain authoritative. It must
finish a valid export before exiting zero. It must not report success merely
because a background export started. No shell command or producer path comes
from a source config or message. Failed, timed-out or interactive-auth-blocked
exports do not start an import. Upstream stdout/stderr is suppressed to avoid
copying credentials or mailbox content into the orchestration result.

Agents invoking a GOG-backed wrapper must use the normal native outside-sandbox
approval for that Google scope. The script grants no new Google access. The
handoff is verified with synthetic producers; this delivery does not claim to
repair Google reauthentication or activate a real unattended Google exporter.
After an interrupted local refresh, rerun the same source; per-record or
append-offset resume is not implemented.

## Future Go decision

A Go port is a separate implementation decision, not a prerequisite for
CrawlBar. If approved later, use Crawlkit for provider-neutral storage helpers,
control data and refresh state, while keeping mail parsing, Gmail identities,
privacy policy and triage in mailcrawl. Do not copy provider-specific behavior
from another crawler or force its schema onto this archive.

Before cutover, the Go candidate must reproduce the synthetic CLI journeys,
schema migrations, row/provenance identities, label reconciliation, human
decision preservation, rollback, import limits and health contract. Test large
archives and actual release platforms before choosing the port. Language choice
alone is not evidence of better performance, lower RAM or stronger security.
