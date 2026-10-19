import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "local_verify", Path(__file__).resolve().parents[1] / "scripts/local-verify.py")
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)
SHA = "1" * 40
PR = {"user": {"login": "TheAngryPit"}, "state": "open",
      "base": {"ref": "main", "sha": "0" * 40,
               "repo": {"full_name": "TheAngryPit/mailcrawl"}},
      "head": {"sha": SHA, "repo": {"full_name": "TheAngryPit/mailcrawl"}}}


class LocalVerificationTests(unittest.TestCase):
    def test_only_owner_same_repo_current_open_pr(self):
        verify.validate_pr(PR, SHA, "TheAngryPit")
        cases = [(["head", "sha"], "2" * 40),
                 (["head", "repo", "full_name"], "fork/mailcrawl"),
                 (["user", "login"], "other"), (["state"], "closed"),
                 (["base", "ref"], "other"),
                 (["base", "repo", "full_name"], "other/mailcrawl")]
        for path, value in cases:
            with self.subTest(path=path):
                pr = copy.deepcopy(PR)
                target = pr
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                with self.assertRaises(ValueError):
                    verify.validate_pr(pr, SHA, "TheAngryPit")
        with self.assertRaises(ValueError):
            verify.validate_pr(PR, SHA, "other")

    def drive(self, mode):
        calls = []
        published = []
        pr_reads = 0

        def api(path):
            nonlocal pr_reads
            if path == "user":
                return {"login": "TheAngryPit"}
            pr_reads += 1
            pr = copy.deepcopy(PR)
            if mode == "stale" and pr_reads > 1:
                pr["head"]["sha"] = "2" * 40
            return pr

        def output(*args):
            if args[:3] == ("git", "rev-parse", "HEAD"):
                return SHA
            if args[:3] == ("git", "status", "--porcelain"):
                return ""
            if args[:3] == ("docker", "image", "inspect"):
                return '[{"Id": "sha256:fixture"}]'
            return "0"

        def run(*args, **kwargs):
            calls.append(args)
            if args[:2] == ("git", "archive"):
                kwargs["stdout"].write(b"synthetic snapshot")
            if args[:2] == ("docker", "start") and mode == "failure":
                raise subprocess.CalledProcessError(1, args)
            if args[:2] == ("docker", "start") and mode == "timeout":
                raise subprocess.TimeoutExpired(args, 1800)
            if args[:2] == ("docker", "cp") and args[-1].endswith("proof-summary.json"):
                Path(args[-1]).write_text('{"status":"pass"}')

        def status(sha, state, number):
            published.append(state)
            return {"context": "verification", "state": state, "url": "https://example.com/status"}

        with tempfile.TemporaryDirectory() as scratch:
            evidence = Path(scratch) / "evidence"
            args = ["local-verify.py", "--pr", "63", "--publish", "--evidence", str(evidence)]
            with patch.object(sys, "argv", args), patch.object(verify, "api", side_effect=api), \
                    patch.object(verify, "output", side_effect=output), \
                    patch.object(verify, "run", side_effect=run), \
                    patch.object(verify, "status", side_effect=status), \
                    patch("builtins.print"):
                if mode == "success":
                    verify.main()
                else:
                    with self.assertRaises((ValueError, subprocess.SubprocessError)):
                        verify.main()
            receipt = json.loads((evidence / "receipt.json").read_text())
        self.assertEqual(published, ["pending", "success" if mode == "success" else "failure"])
        self.assertEqual(receipt["status"], "success" if mode == "success" else "failure")
        create = next(c for c in calls if c[:2] == ("docker", "create"))
        self.assertIn("--network=none", create)
        self.assertNotIn("--env", create)
        self.assertNotIn("--volume", create)
        self.assertTrue(any(c[:2] == ("docker", "stop") for c in calls))
        self.assertTrue(any(c[:2] == ("docker", "rm") for c in calls))

    def test_success_requires_full_matrix_and_current_head(self):
        self.drive("success")

    def test_failure_never_publishes_success(self):
        self.drive("failure")

    def test_head_change_invalidates_completed_tests(self):
        self.drive("stale")

    def test_timeout_reports_failure_and_stops_owned_container(self):
        self.drive("timeout")


@unittest.skipUnless(shutil.which("jq"), "hosted reporter requires jq")
class HostedReporterTests(unittest.TestCase):
    def test_only_two_successful_jobs_publish_success(self):
        workflow = Path(__file__).resolve().parents[1] / ".github/workflows/verification.yml"
        body = workflow.read_text().split("        run: |\n", 1)[1]
        body = "\n".join(line[10:] for line in body.splitlines())
        cases = [(["success", "success"], "success", "success", False, False),
                 (["success", "failure"], "failure", "failure", False, False),
                 (["success", "skipped"], "success", "failure", False, False),
                 (["success"], "success", "failure", False, False),
                 (["success", "success"], "cancelled", "failure", False, False),
                 (["success", "success"], "success", "failure", True, False),
                 (["success", "success"], "success", "failure", False, True)]
        with tempfile.TemporaryDirectory() as scratch:
            stub = Path(scratch) / "gh"
            stub.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                            "if '--method' in sys.argv:\n"
                            "    print(sys.argv[sys.argv.index('context=verification')+2])\n"
                            "elif 'contents/' in sys.argv[2]:\n"
                            "    print('changed' if os.environ['FIXTURE_DRIFT'] == '1' and 'ref=main' not in sys.argv[2] else 'approved')\n"
                            "else:\n"
                            "    for job in json.loads(os.environ['FIXTURE_JOBS']):\n"
                            "        print(json.dumps(job))\n")
            stub.chmod(0o700)
            for conclusions, conclusion, expected, duplicate, drift in cases:
                with self.subTest(conclusions=conclusions, conclusion=conclusion):
                    jobs = [dict(name=name, conclusion=value) for name, value in
                            zip(["check", "check" if duplicate else "e2e"], conclusions)]
                    env = dict(os.environ, PATH=scratch + os.pathsep + os.environ["PATH"],
                               FIXTURE_JOBS=json.dumps(jobs), CONCLUSION=conclusion,
                               FIXTURE_DRIFT="1" if drift else "0",
                               GH_REPO="fixture/repo", RUN_ID="1", HEAD_SHA=SHA,
                               RUN_URL="https://example.com/run")
                    result = subprocess.run(["bash", "-c", body], env=env,
                                            capture_output=True, text=True, check=True)
                    self.assertIn("state=" + expected, result.stdout)
