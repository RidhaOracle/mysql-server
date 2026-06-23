#!/usr/bin/env bash
# demo-labeler.sh — exercise the PR auto-labeler for a stakeholder showcase.
#
# Appends one clearly-marked throwaway comment to a real file in each labeled
# area, so a single push lights up every "area:*" label on the PR. This is a
# DEMO commit: revert it before merge (see the printed instructions at the end).
#
# Usage (run from a full mysql-server checkout, on your PR branch):
#   bash scripts/dev/demo-labeler.sh          # stage + commit the demo edits
#   bash scripts/dev/demo-labeler.sh --push   # ...and push to origin
set -euo pipefail

MARK="OCA/labeler demo — revert before merge"

# label -> glob (mirrors .github/labeler.yml). One representative file each.
globs=(
  "storage/innobase/**:area:innodb"
  "sql/sql_optimizer*:area:optimizer"
  "sql/rpl_*:area:replication"
  "client/**:area:client"
  "plugin/**:area:pluggable"
  "cmake/**:area:build"
  "mysql-test/**:area:tests"
  "docs/**:area:docs"
)

safe_ext() { case "$1" in *.cc|*.cpp|*.cxx|*.c|*.h|*.hpp|*.ic) echo "//";; \
  *.cmake|*.sh|*.txt|*.cnf) echo "#";; *.md) echo "<!--md-->";; *) echo "";; esac; }

touched=0
for entry in "${globs[@]}"; do
  glob="${entry%%:*}"; label="${entry#*:}"
  # first tracked file matching the glob that has a comment-safe extension
  file=""
  while IFS= read -r f; do
    pre="$(safe_ext "$f")"; [ -n "$pre" ] && { file="$f"; prefix="$pre"; break; }
  done < <(git ls-files -- $glob 2>/dev/null)
  if [ -z "$file" ]; then echo "skip  $label (no comment-safe file matched $glob)"; continue; fi
  if [ "$prefix" = "<!--md-->" ]; then
    printf '\n<!-- %s -->\n' "$MARK" >> "$file"
  else
    printf '\n%s %s\n' "$prefix" "$MARK" >> "$file"
  fi
  echo "touch $label -> $file"
  touched=$((touched+1))
done

[ "$touched" -eq 0 ] && { echo "nothing touched"; exit 1; }
git add -A
git commit -q -m "demo: exercise PR auto-labeler across areas (revert before merge)"
echo "committed demo edits ($touched files)."

if [ "${1:-}" = "--push" ]; then
  git push origin HEAD
  echo "pushed."
fi

cat <<'EOF'

To undo this demo commit after the screenshots:
  git revert --no-edit HEAD        # keeps history, or
  git reset --hard HEAD~1 && git push --force-with-lease
EOF
