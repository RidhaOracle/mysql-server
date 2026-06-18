#!/usr/bin/env bash
# Run MySQL Test Run the same way CI does. Usage:
#   mtr.sh smoke              curated fast subset (≈ the PR check)
#   mtr.sh main               full main suite
#   mtr.sh --suite=innodb ... raw args passed straight to ./mtr
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
BUILD_DIR="${BUILD_DIR:-$REPO_ROOT/build}"
cd "$BUILD_DIR/mysql-test"

case "${1:-smoke}" in
  smoke)
    # Fast signal: a representative slice across major areas, parallelized.
    exec ./mtr --parallel=auto --force --suite=main \
      --do-test-list="$REPO_ROOT/mysql-test/collections/smoke.list"
    ;;
  main)
    exec ./mtr --parallel=auto --force --suite=main
    ;;
  *)
    exec ./mtr "$@"
    ;;
esac
