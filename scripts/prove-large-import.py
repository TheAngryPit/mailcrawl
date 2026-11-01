#!/usr/bin/env python3
import argparse
import json
import os
import resource
import sys
import tempfile
from pathlib import Path
from contextlib import closing

from mailcrawl.ingest import import_mbox
from mailcrawl.store import connect, init_account


def flush_fixture(stream):
    stream.flush()
    os.fsync(stream.fileno())
    if hasattr(os, "posix_fadvise"):
        os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)


def main():
    parser = argparse.ArgumentParser(
        description="Generate and import a multi-GiB synthetic mbox; retain only a JSON receipt."
    )
    parser.add_argument("--minimum-mib", type=int, default=2049)
    parser.add_argument("--max-rss-mib", type=int, default=256)
    args = parser.parse_args()
    if args.minimum_mib <= 512 or args.max_rss_mib <= 0:
        parser.error("proof must exceed 512 MiB and have a positive RSS limit")
    header = (b"From 1768000000000000001@xxx Thu Jan 15 12:00:00 +0000 2026\n"
              b"X-GM-MSGID: 1768000000000000001\nX-GM-THRID: 1768000000000000001\n"
              b"X-Gmail-Labels: Inbox\nFrom: Fixture <fixture@example.com>\n"
              b"To: Ada <ada@example.com>\nSubject: Synthetic large archive\n"
              b"Date: Thu, 15 Jan 2026 12:00:00 +0000\n"
              b"Message-ID: <large-proof@example.com>\nContent-Type: text/plain\n\n")
    block = header + (b"x" * 4096 + b"\n") * 256 + b"\n"
    records = (args.minimum_mib * 1024 * 1024 + len(block) - 1) // len(block)
    print("Generating synthetic archive", file=sys.stderr, flush=True)
    with tempfile.TemporaryDirectory(prefix="mailcrawl-large-proof-") as folder:
        root = Path(folder)
        source = root / "synthetic.mbox"
        with source.open("wb") as stream:
            for number in range(records):
                stream.write(block)
                if (number + 1) % 32 == 0:
                    flush_fixture(stream)
            flush_fixture(stream)
        size = source.stat().st_size
        print(f"Generated {size} bytes; starting import", file=sys.stderr, flush=True)
        db = init_account(root / "archive", "personal", "ada@example.com")
        with closing(connect(db)) as conn:
            result = import_mbox(conn, alias="personal", mbox_path=source,
                                 labels_path=None, taxonomy_path=None,
                                 account_email="ada@example.com")
            assert result.n_seen == records
            assert result.n_new == 1 and result.n_dup == records - 1
            assert result.n_error == 0 and result.n_refused == 0
            assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_mib = rss / (1024 * 1024 if sys.platform == "darwin" else 1024)
        assert rss_mib < args.max_rss_mib, f"RSS {rss_mib} exceeded proof limit"
        print(json.dumps({"status": "pass", "synthetic": True,
                          "source_bytes": size, "records_seen": records,
                          "new_messages": result.n_new, "duplicates": result.n_dup,
                          "peak_rss_mib": round(rss_mib, 2),
                          "rss_limit_mib": args.max_rss_mib,
                          "scope": "streaming repeated message; not a distinct-message throughput benchmark"}))


if __name__ == "__main__":
    main()
