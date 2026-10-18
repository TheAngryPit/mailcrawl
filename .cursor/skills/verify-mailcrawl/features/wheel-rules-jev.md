# Wheel, bundled rules, and jev redirect

The installed wheel carries `rules-v0.json`. `rules` reports `rules_version` `rules-v4` from that file. A jev client that is redirected to a second loopback host refuses the redirect and does not send the key there.

## Sub-features

- `wheel`. `mailcrawl.__file__` is under `site-packages`.
- `bundled-rules`. `bundled_rules_path` points inside that install, and `rules` prints `rules-v4`.
- `redirect`. The first loopback host returns 302. The command fails with a redirect error. The second host records no request.

## How to get to it (user POV)

- Install the wheel with no index and no extra dependencies.
- Point config `rules.path` at the bundled file and run `rules` on a synthetic account.
- Point the jev client at `127.0.0.1` and answer with a redirect to another `127.0.0.1` port.

## Driving it with mailcrawl-verify

Proof type: command transcript.

Preconditions:

- Doctor exited 0.
- `pyproject.toml` and `mailcrawl/rules-v0.json` exist.

- Run `"$REPO_ROOT/.cursor/skills/verify-mailcrawl/bin/mailcrawl-verify" drive --feature wheel-rules-jev`.
- Expect exit 0. The log names fixture id `bundled-rules-v0` and test name `test_redirect_drops_the_key_before_the_second_host`.
- When the package files are missing, the same command exits 2.

## Gotchas

- The jev key in this drive is the literal `loopback-secret`. It is not a credential.
- The drive does not contact `api.typesafe.ai`. Both hosts are `127.0.0.1`.
- Proxy variables point at `127.0.0.1:9`. The jev client clears proxies for its own request.
