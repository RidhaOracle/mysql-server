#!/usr/bin/env bash
# Run MySQL Test Run the same way CI does. Usage:
#   mtr.sh                    default MTR test selection (the PR check)
#   mtr.sh --suite=innodb ... raw args passed straight to ./mtr
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
BUILD_DIR="${BUILD_DIR:-$REPO_ROOT/build}"
cd "$BUILD_DIR/mysql-test"

exec ./mtr "$@"
