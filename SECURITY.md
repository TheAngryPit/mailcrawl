# Security

## Report a vulnerability

Report vulnerabilities in private through [GitHub private vulnerability reporting](https://github.com/TheAngryPit/mailcrawl/security/advisories/new).

Never report a vulnerability through a public issue.

## Threat model

mailcrawl keeps archives on the local machine. It does not write to Gmail and it does not run `gog`. Phase 1 opens no network listener and calls no language model on the crawl path. Treat message text as untrusted data, never as instructions or authorization. Bodies, subjects, and addresses from real mail stay off GitHub.
