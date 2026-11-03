#!/usr/bin/env bash
# Copyright (c) 2026, Oracle and/or its affiliates.
set -euo pipefail
if ! git diff --quiet --no-ext-diff --no-textconv "${FORMAT_BASE:-HEAD^1}" HEAD -- \
  '.clang-format'; then
  echo "::error::The repository formatting policy requires trusted review."
  exit 1
fi

changed_files="$RUNNER_TEMP/clang-format-files"
git diff --name-only -z --diff-filter=ACMR \
  --no-ext-diff --no-textconv "${FORMAT_BASE:-HEAD^1}" HEAD -- \
  '*.c' '*.cc' '*.cpp' '*.h' '*.hpp' > "$changed_files"

found=0
fail=0
while IFS= read -r -d '' file; do
  found=1
  [ -f "$file" ] || continue
  if ! clang-format-15 --style=file --dry-run --Werror -- "$file"; then
    fail=1
  fi
done < "$changed_files"

if [ "$found" -eq 0 ]; then
  echo "No C/C++ changes."
fi
if [ "$fail" -ne 0 ]; then
  echo "::error::Run scripts/ci/format.sh to fix formatting."
  exit 1
fi
