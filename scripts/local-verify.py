#!/usr/bin/env python3
"""Run the mailcrawl CI matrix locally; optionally publish the owner's result."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY = "TheAngryPit/mailcrawl"
OWNER = "TheAngryPit"
CONTEXT = "verification"


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def output(*args):
    return run(*args, capture_output=True, text=True).stdout.strip()


def api(path, payload=None):
    args = ["gh", "api", path]
    if payload is not None:
        args += ["--method", "POST", "--input", "-"]
    return json.loads(run(*args, input=json.dumps(payload) if payload else None,
                          capture_output=True, text=True).stdout)


def validate_pr(pr, sha, viewer):
    if viewer != OWNER or pr["user"]["login"] != OWNER:
        raise ValueError("Local publication is restricted to TheAngryPit and their own PRs")
    if pr["state"] != "open" or pr["base"]["ref"] != "main":
        raise ValueError("Expected an open PR targeting main")
    if pr["base"]["repo"]["full_name"] != REPOSITORY:
        raise ValueError("Unexpected base repository")
    if (pr["head"]["repo"] or {}).get("full_name") != REPOSITORY:
        raise ValueError("Local publication does not accept fork PRs")
    if pr["head"]["sha"] != sha:
        raise ValueError("PR head changed or does not match the tested commit")


def status(sha, state, number):
    return api(f"repos/{REPOSITORY}/statuses/{sha}", {
        "state": state, "context": CONTEXT,
        "description": f"Local / {OWNER}: " + {
            "pending": "isolated Linux CI matrix running",
            "success": "full isolated Linux CI matrix passed",
            "failure": "verification failed; inspect local receipt",
        }[state],
        "target_url": f"https://github.com/{REPOSITORY}/pull/{number}",
    })


def stop_container(name):
    run("docker", "stop", "--time", "5", name, stdout=subprocess.DEVNULL)
    run("docker", "rm", "-v", name, stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--image", default="mailcrawl-local-ci:1")
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    if output("git", "status", "--porcelain"):
        raise ValueError("Commit changes first; verification requires a clean checkout")
    sha = output("git", "rev-parse", "HEAD")
    pr_path = f"repos/{REPOSITORY}/pulls/{args.pr}"
    viewer = api("user")["login"]
    pr = api(pr_path)
    validate_pr(pr, sha, viewer)
    run("git", "merge-base", "--is-ancestor", pr["base"]["sha"], sha)
    image = json.loads(output("docker", "image", "inspect", args.image))[0]["Id"]
    args.evidence.mkdir(parents=True, exist_ok=False, mode=0o700)
    receipt = {"schema": "mailcrawl.local-verification.v1", "repository": REPOSITORY,
               "sha": sha, "base_sha": pr["base"]["sha"], "actor": viewer,
               "image": image, "network": "none", "credentials": "not mounted",
               "started": datetime.now(timezone.utc).isoformat(), "status": "pending"}
    archive = args.evidence / "source.tar"
    with archive.open("wb") as stream:
        run("git", "archive", "--format=tar", sha, stdout=stream)
    receipt["source_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    name = "mailcrawl-local-" + uuid.uuid4().hex
    created = False
    published_pending = False
    try:
        if args.publish:
            status(sha, "pending", args.pr)
            published_pending = True
        run("docker", "create", "--name", name, "--network=none", "--memory=1g",
            "--cpus=2", "--pids-limit=128", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", image, "bash", "-euc",
            "mkdir /work; tar -xf /tmp/source.tar -C /work; bash /work/scripts/local-ci.sh",
            stdout=subprocess.DEVNULL)
        created = True
        run("docker", "cp", str(archive), name + ":/tmp/source.tar")
        with (args.evidence / "matrix.log").open("w") as log:
            run("docker", "start", "--attach", name, stdout=log,
                stderr=subprocess.STDOUT, timeout=1800)
        exit_code = int(output("docker", "inspect", "--format", "{{.State.ExitCode}}", name))
        receipt["matrix_exit_code"] = exit_code
        if exit_code:
            raise RuntimeError(f"CI matrix exited {exit_code}")
        run("docker", "cp", name + ":/work/proof/summary.json",
            str(args.evidence / "proof-summary.json"))
        if json.loads((args.evidence / "proof-summary.json").read_text())["status"] != "pass":
            raise ValueError("Scaffold proof did not pass")
        if output("git", "rev-parse", "HEAD") != sha or output("git", "status", "--porcelain"):
            raise ValueError("Checkout changed during verification")
        current = api(pr_path)
        validate_pr(current, sha, api("user")["login"])
        if current["base"]["sha"] != receipt["base_sha"]:
            raise ValueError("Base changed during verification; rerun against the new base")
        receipt["status"] = "success"
        receipt["log_sha256"] = hashlib.sha256((args.evidence / "matrix.log").read_bytes()).hexdigest()
        (args.evidence / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        if args.publish:
            result = status(sha, "success", args.pr)
            if result["context"] != CONTEXT or result["state"] != "success":
                raise ValueError("GitHub did not confirm the expected status")
            receipt["status_url"] = result["url"]
    except BaseException:
        receipt["status"] = "failure"
        if published_pending:
            status(sha, "failure", args.pr)
        raise
    finally:
        receipt["finished"] = datetime.now(timezone.utc).isoformat()
        (args.evidence / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        if created:
            stop_container(name)
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"local verification failed: {error}", file=sys.stderr)
        sys.exit(1)
