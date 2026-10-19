"""Synthetic, offline regressions for bundled developer tools."""
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WIZARD = ROOT / '.cursor/skills/wizard/template.sh'
VERIFY = ROOT / '.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify'


class ToolSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.wizard = self.base / 'wizard-library.sh'
        self.wizard.write_text(WIZARD.read_text().rsplit('run_wizard "$@"; exit', 1)[0])

    def write_env(self, destination, value="dummy-'secret"):
        env = dict(os.environ, ENV_FILE=str(destination))
        return subprocess.run(
            ['bash', '-c', 'source "$1"; value=$(cat); write_env TEST_TOKEN "$value"', 'fixture', str(self.wizard)],
            input=value, text=True, capture_output=True, env=env, cwd=self.base,
        )

    def test_new_and_existing_private_env_preserve_values(self):
        destination = self.base / '.env'
        first = self.write_env(destination)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self.assertIn("TEST_TOKEN='dummy-'\\''secret'", destination.read_text())
        second = self.write_env(destination, 'next-dummy')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(destination.read_text(), "TEST_TOKEN='next-dummy'\n")
        self.assertNotIn('next-dummy', second.stdout + second.stderr)

    def test_readable_env_is_refused_without_secret_or_mutation(self):
        destination = self.base / '.env'
        destination.write_text('KEEP=yes\n')
        destination.chmod(0o644)
        result = self.write_env(destination)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(destination.read_text(), 'KEEP=yes\n')
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o644)
        self.assertNotIn("dummy-'secret", result.stdout + result.stderr)

    def test_symlink_and_symlink_parent_are_refused(self):
        target = self.base / 'target'
        target.write_text('KEEP=yes\n')
        target.chmod(0o600)
        link = self.base / '.env'
        link.symlink_to(target)
        self.assertNotEqual(self.write_env(link).returncode, 0)
        alias = self.base / 'alias'
        alias.symlink_to(self.base, target_is_directory=True)
        self.assertNotEqual(self.write_env(alias / 'new.env').returncode, 0)
        self.assertEqual(target.read_text(), 'KEEP=yes\n')
        self.assertFalse((self.base / 'new.env').exists())

    def test_writable_parent_is_refused(self):
        directory = self.base / 'writable'
        directory.mkdir(mode=0o777)
        directory.chmod(0o777)
        self.assertNotEqual(self.write_env(directory / '.env').returncode, 0)
        self.assertFalse((directory / '.env').exists())

    @unittest.skipUnless(os.uname().sysname == 'Darwin', 'macOS ACL fixture')
    def test_acl_env_is_refused(self):
        destination = self.base / '.env'
        destination.write_text('KEEP=yes\n')
        destination.chmod(0o600)
        subprocess.run(['chmod', '+a', 'everyone allow read', str(destination)], check=True)
        try:
            result = self.write_env(destination)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(destination.read_text(), 'KEEP=yes\n')
        finally:
            subprocess.run(['chmod', '-N', str(destination)], check=True)

    def test_verifier_ignores_planted_scratch_and_scopes_cleanup(self):
        library = self.base / 'verify-library.sh'
        library.write_text(VERIFY.read_text().rsplit('main "$@"', 1)[0])
        planted = self.base / 'hostile'
        interpreter = planted / 'venv/bin/python'
        interpreter.parent.mkdir(parents=True)
        marker = self.base / 'planted-ran'
        interpreter.write_text('#!/bin/sh\ntouch "$PLANTED_MARKER"\n')
        interpreter.chmod(0o755)
        env = dict(os.environ, SCRATCH_DIR=str(planted), RUN_ID='../hostile', PLANTED_MARKER=str(marker))
        fakebin = self.base / 'bin'
        fakebin.mkdir()
        python = fakebin / 'python3'
        python.write_text("#!/bin/bash\nset -eu\n[[ \"$1 $2 $3\" == '-m venv --copies' ]]\nmkdir -p \"$4/bin\"\ncat > \"$4/bin/python\" <<'SH'\n#!/bin/bash\nprintf '%s\\n' \"$*\" >> \"$TEST_PIP_LOG\"\nSH\nchmod 700 \"$4/bin/python\"\n")
        python.chmod(0o700)
        piplog = self.base / 'pip.log'
        env.update(PATH=str(fakebin) + os.pathsep + env['PATH'], TEST_PIP_LOG=str(piplog))
        script = 'source "$1"; printf "%s\\n" "$SCRATCH_DIR"; stat -f %Lp "$SCRATCH_DIR" 2>/dev/null || stat -c %a "$SCRATCH_DIR"; ensure_wheel; ensure_wheel; cmd_cleanup'
        processes = [subprocess.Popen(['bash', '-c', script, 'fixture', str(library)], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            results.append(subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr))
        self.assertEqual(len(piplog.read_text().splitlines()), 4)
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = result.stdout.splitlines()
            self.assertNotEqual(lines[0], str(planted))
            self.assertIn('700', lines)
            self.assertFalse(Path(lines[0]).exists())
        self.assertNotEqual(results[0].stdout.splitlines()[0], results[1].stdout.splitlines()[0])
        self.assertTrue(interpreter.exists())
        self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()
