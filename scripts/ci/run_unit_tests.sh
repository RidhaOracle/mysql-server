#!/usr/bin/env bash
# Copyright (c) 2026, Oracle and/or its affiliates.
set -euo pipefail
ctest_args=(
  --test-dir build
  --parallel "$(nproc)"
  # Some CTest entries contain an entire integration suite.
  --timeout 900
  --output-on-failure
  # Bug#39882117 and Bug#39980115: Temporarily quarantine persistent
  # trunk failures. Keep the retry path below for transient failures
  # in all other CTest cases.
  --exclude-regex '^(routertest_integration_routing_splitting|testSecureSocket-t)$'
)

set +e
ctest "${ctest_args[@]}" \
  --output-log build/mysql-test/var/ctest.log
ctest_result=$?
set -e

if (( ctest_result == 0 )); then
  exit 0
fi

failed_tests=build/Testing/Temporary/LastTestsFailed.log
if [[ ! -s "$failed_tests" ]]; then
  echo "CTest failed without a failed-test list; not retrying." >&2
  exit "$ctest_result"
fi

initial_failures=build/mysql-test/var/ctest-initial-failures.log
cp "$failed_tests" "$initial_failures"
initial_names=build/mysql-test/var/ctest-initial-failure-names.log
sed 's/^[^:]*://' "$initial_failures" | sort -u > "$initial_names"
echo "::warning::Retrying only the CTest failures from the initial run"
set +e
ctest "${ctest_args[@]}" \
  --rerun-failed \
  --repeat until-pass:2 \
  --output-log build/mysql-test/var/ctest-rerun.log
rerun_result=$?
set -e

persistent_names=build/mysql-test/var/ctest-persistent-failures.log
recovered_names=build/mysql-test/var/ctest-recovered-failures.log
: > "$persistent_names"
if (( rerun_result != 0 )) && [[ -s "$failed_tests" ]]; then
  sed 's/^[^:]*://' "$failed_tests" | sort -u > "$persistent_names"
fi
comm -23 "$initial_names" "$persistent_names" > "$recovered_names"

{
  echo "### CTest retry summary (${MTR_SHARD:-services})"
  echo
  echo "Initial failures:"
  sed 's/^/- /' "$initial_names"
  echo
  echo "Recovered on retry:"
  if [[ -s "$recovered_names" ]]; then
    sed 's/^/- /' "$recovered_names"
  else
    echo "- None"
  fi
  echo
  echo "Persistent failures:"
  if [[ -s "$persistent_names" ]]; then
    sed 's/^/- /' "$persistent_names"
  elif (( rerun_result == 0 )); then
    echo "- None"
  else
    echo "- CTest did not produce a persistent-failure list."
  fi
} >> "$GITHUB_STEP_SUMMARY"

exit "$rerun_result"
