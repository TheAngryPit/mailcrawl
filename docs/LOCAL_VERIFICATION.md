# Local or hosted verification

The required `verification` commit status accepts two routes. Hosted `check` and
`e2e` jobs remain visible; the trusted `verification-report` workflow publishes
the gate only after finding exactly one successful job of each name and checking
that `check.yml` matches its approved default-branch definition. Workflow changes
require the reviewed owner-operated local route. The reporter never checks
out PR code or supplies it with a write token. It starts operating after this
workflow is merged into the default branch.

For an owner-operated local run, build the dependency image once:

```sh
docker build -f scripts/local-ci.Dockerfile -t mailcrawl-local-ci:1 .
```

Review and commit the candidate, push the branch, then run from that checkout:

```sh
python3 scripts/local-verify.py --pr 63 --publish \
  --evidence /tmp/mailcrawl-verification-unique-run
```

Use the actual PR number and a new evidence directory. Without `--publish`, the
same run produces only local evidence. `[skip ci]` can suppress hosted execution
for an explicitly local run; it does not itself satisfy any check.

The runner accepts only an open PR authored by `TheAngryPit` in
`TheAngryPit/mailcrawl`, targeting `main`, with the authenticated viewer also
`TheAngryPit`. The clean checkout must match the remote head and contain its
current base. It exports only committed files and runs in a disposable Linux
container with no external network, host mounts, Docker socket or credentials.
It limits the matrix to 30 minutes and stops only its uniquely named container
on completion, failure or interruption.
The dependency image is prepared with network access before verification.

The matrix runs `make check` without and with sqlite-vec, `scripts/proof.sh`, the
wheel quickstart, and a fresh-wheel end-to-end journey. A result is published
only after all commands succeed and the checkout, PR head and base are checked
again. Failed runs report failure. Receipt, log and source hashes identify the
tested commit and image; local logs remain at the evidence path. A new PR head
requires a new result. The ruleset's strict base policy also requires the PR to
remain current with `main`. Agents never merge.

## Trust boundary

The status description identifies `Local / TheAngryPit` or `Hosted CI`, rather
than pretending a local run was GitHub Actions. Only commit statuses use the
name `verification`; no Actions job has that name. GitHub can otherwise require
both a check and a commit status when their names collide.

Allowing local publication requires accepting statuses without a GitHub Actions
source restriction. GitHub allows repository writers to create these statuses.
The runner's owner checks are operational safeguards, not a server-enforced
identity restriction or a cryptographic attestation. This route assumes all
repository writers and installed integrations are trusted. Reassess it before
adding collaborators or integrations with status-write access. A dedicated
GitHub App would be needed for stronger publisher provenance.

The head snapshot is tested, including the recorded current base; this is not
an independent test of a future merge against a base that has since changed.
Local dependency versions are pinned in the image recipe, while hosted jobs
resolve their existing dependency ranges. These are the same command matrix,
not a claim of byte-identical hosted environments or a security certification.
