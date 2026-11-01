#!/usr/bin/env python3
"""Drive the installed console CLI with synthetic local mail, outside the checkout."""
import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mailcrawl-operations-journey-") as folder:
        root = Path(folder)
        export = root / "export"
        shutil.copytree(args.repo / "testdata/fixtures/gog-export/native", export)
        config = root / "config.toml"
        config.write_text('[accounts.thehive]\nemail="ada@example.com"\n'
                          '[sources.daily]\naccount="thehive"\nkind="gog-export"\npath="export"\n')
        base = ["mailcrawl", "--json", "--home", str(root / "archive"), "--config", str(config)]

        def call(arguments):
            result = subprocess.run(base + arguments, cwd=root, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError("synthetic CLI failed: " + repr(arguments))
            return json.loads(result.stdout)

        version = subprocess.check_output(["mailcrawl", "--version"], cwd=root, text=True).strip()
        call(["init", "--account", "thehive", "--email", "ada@example.com"])
        manifest = call(["metadata", "--json"])
        results = {}
        for command in ("sync", "status", "doctor", "search"):
            argv = [arg.replace("{config:account}", "thehive")
                    for arg in manifest["commands"][command]["argv"][1:]]
            if command == "search":
                argv.append("synthetic")
            results[command] = call(argv)
        assert results["sync"]["sources"][0]["n_new"] == 3
        assert results["status"]["state"] == results["doctor"]["state"] == "ready"
        assert results["status"]["databases"][0]["id"] == "thehive"
        repeat = call(["sync", "--source", "daily", "--json"])
        assert repeat["sources"][0]["n_new"] == 0
        assert repeat["sources"][0]["n_dup"] == 3
        print(json.dumps({"status": "pass", "synthetic": True, "version": version,
                          "commands": ["init", "metadata", *results],
                          "new_messages": 3, "repeat_duplicates": 3,
                          "scope": "installed CLI contract; no native CrawlBar or Google access"}))


if __name__ == "__main__":
    main()
