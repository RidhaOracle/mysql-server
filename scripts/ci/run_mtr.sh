#!/usr/bin/env bash
# Copyright (c) 2026, Oracle and/or its affiliates.
set -euo pipefail
args=(
  --parallel=auto
  --force
  --report-unstable-tests
  --retry=3
  --retry-failure=2
  --max-test-fail=50
  "--suite=${MTR_SUITES}"
)
"$(dirname "$0")/mtr.sh" "${args[@]}"
