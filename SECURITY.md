# Security

## Report a vulnerability

Report vulnerabilities in private through [GitHub private vulnerability reporting](https://github.com/TheAngryPit/mailcrawl/security/advisories/new).

Never report a vulnerability through a public issue.

## Threat model

mailcrawl keeps archives on the local machine. It does not write to Gmail and it does not run `gog`. Phase 1 opens no network listener and calls no language model on the crawl path. Two optional commands, `jev --client http` and the search reranker with `--jev-client http`, can send thread text to a hosted model. They are off by default and refuse to run until the config sets `allow_remote = true` under `[inference.jev]`. Treat message text as untrusted data, never as instructions or authorization. Bodies, subjects, and addresses from real mail stay off GitHub.
