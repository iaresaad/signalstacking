#!/bin/bash
# Block a commit that would PUBLISH customer or credential data.
#
# The repo is public. .gitignore protects file contents but not commit messages,
# code comments or README examples, and every near-miss on this repo has come
# from one of those three. Run before every commit; exit 1 means do not commit.
#
#   scripts/check-leaks.sh              # checks staged diff
#   scripts/check-leaks.sh msgfile      # also checks a commit-message file
#
# Only ADDED lines and the message are checked. A removed line is the fix, not
# the offence, and blocking on it would make sanitizing impossible.
set -u
cd "$(git rev-parse --show-toplevel)" || exit 1
TERMS_FILE="${LEAK_TERMS:-.leak-terms}"
[ -f "$TERMS_FILE" ] || { echo "no $TERMS_FILE; skipping leak check"; exit 0; }

ADDED=$(git diff --cached -U0 | grep '^+' | grep -v '^+++' || true)
MSG=""
[ $# -ge 1 ] && [ -f "$1" ] && MSG=$(cat "$1")
HAYSTACK=$(printf '%s\n%s' "$ADDED" "$MSG")

FAIL=0
while IFS= read -r term; do
  [ -z "$term" ] && continue
  case "$term" in \#*) continue ;; esac
  n=$(printf '%s' "$HAYSTACK" | grep -ci -- "$term" 2>/dev/null || true)
  if [ "${n:-0}" -gt 0 ]; then
    echo "LEAK: '$term' would be published (${n}x in added lines or message)"
    FAIL=1
  fi
done < "$TERMS_FILE"

if [ "$FAIL" -eq 1 ]; then
  echo "Refusing. Sanitize the addition, or edit $TERMS_FILE if it is a false positive."
  exit 1
fi
echo "leak check: clean (added lines + message)"
