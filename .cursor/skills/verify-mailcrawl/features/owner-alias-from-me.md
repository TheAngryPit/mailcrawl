# Owner alias

An `owner_addresses` entry counts as the owner only when the first Authentication-Results block is Gmail's own pass. `from-me` reports that count and does not write. Rules then see the alias.

## Sub-features

- `import`. A synthetic mbox message from `Other@Example.com` with a Gmail pass is stored.
- `from-me`. The dry run reports `alias` 1 and `applied` false.
- `rules`. The bundled rules file runs on that account.

## How to get to it (user POV)

- Install the wheel and init `vcl` with `owner_addresses` set to `Other@Example.com`.
- Import fixture `alias-pass.mbox`.
- Run `from-me` and then `rules`.

## Driving it with mailcrawl-verify

Proof type: command transcript.

Preconditions:

- Doctor exited 0.
- `pyproject.toml` and `mailcrawl/rules-v0.json` exist.

- Run `"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature owner-alias-from-me`.
- Expect exit 0. The log names fixture id `alias-pass.mbox` and test name `test_import_trusts_gmail_pass_and_rules_see_the_alias`.
- When the package files are missing, the same command exits 2.

## Gotchas

- The mbox is written in the temp home during the drive. It is synthetic. Real addresses stay out of the repository.
- The drive does not run `gog` and does not write to Gmail.
- `from-me` without `--apply` changes nothing.
