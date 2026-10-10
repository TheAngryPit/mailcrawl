#!/usr/bin/env bash
# Tracked-file and secret gate. No network. No credentials.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

fail=0

for f in README.md docs/SPEC.md SECURITY.md CONTRIBUTING.md LICENSE Makefile scripts/proof.sh; do
  if [[ ! -f "$f" ]]; then
    echo "missing $f"
    fail=1
  fi
done

title='mailcrawl 📬 — Your inboxes, on the record. Locally.'
first="$(head -n 1 README.md)"
if [[ "$first" != "# $title" ]]; then
  echo "README title line is not the scaffold title"
  fail=1
fi

while IFS= read -r path; do
  base="${path##*/}"
  case "$path" in
    testdata/*) ;;
    *)
      case "$base" in
        .env*|*.db|*.db-wal|*.db-shm|*.sqlite|*.sqlite3|*.eml|*.mbox|*.jsonl|*credential*.json|client_secret*.json|*oauth*token*.json|token.json|tokens.json|age.key|*.age|*.pem|*.gguf|*.safetensors|*.onnx|*.npy)
          echo "tracked forbidden file: $path"
          fail=1
          ;;
      esac
      ;;
  esac
done < <(git ls-files)

# git grep exits 1 when nothing matches. That is success.
grep_status=0
git grep -I -n -E -e 'AKIA[0-9A-Z]{16}|-----BEGIN (RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[0-9A-Za-z-]{10,}|AGE-SECRET-KEY-[A-Z0-9]{20,}' -- . || grep_status=$?
if [[ "$grep_status" -eq 0 ]]; then
  echo "possible secret material in tracked files"
  fail=1
elif [[ "$grep_status" -ne 1 ]]; then
  echo "git grep failed with status $grep_status"
  exit "$grep_status"
fi

ignored_ok() {
  local path="$1"
  if ! git check-ignore --no-index -q -- "$path"; then
    echo "expected ignore rule to match $path"
    fail=1
  fi
}

tracked_ok() {
  local path="$1"
  if git check-ignore --no-index -q -- "$path"; then
    echo "synthetic fixture must stay tracked: $path"
    fail=1
  fi
}

ignored_ok "MailVault/export/note.eml"
ignored_ok "local-archive.db"
ignored_ok "local-archive.db-wal"
ignored_ok "local-archive.db-shm"
ignored_ok "backup.jsonl"
ignored_ok "models/embed.gguf"
ignored_ok ".env"
ignored_ok "age.key"
tracked_ok "testdata/fixtures/gog-export/v0/note.eml"
tracked_ok "testdata/fixtures/gog-export/v0/export.json"
tracked_ok "testdata/fixtures/example.db"
tracked_ok "testdata/fixtures/example.jsonl"

if git ls-files -- 'taxonomy-account-maps.json' '*taxonomy-account-maps.json' | grep -q .; then
  echo "tracked forbidden catalogue taxonomy-account-maps.json"
  fail=1
fi

if [[ "$fail" -ne 0 ]]; then
  exit "$fail"
fi
echo "docs and secret check passed"
