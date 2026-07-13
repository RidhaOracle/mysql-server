# Building MySQL Server from source

The canonical reference is <https://dev.mysql.com/doc/dev/mysql-server/latest/>.
This page is the *fast path* for contributors and is kept in sync with what CI
runs, so following it reproduces the automated checks locally.

## Native Ubuntu 24.04

```bash
scripts/ci/bootstrap.sh        # toolchain + libs
scripts/ci/build.sh debug      # configure (Ninja) + build into build/
```

`build.sh` invokes CMake with `-DDOWNLOAD_BOOST=1 -DWITH_BOOST=~/.cache/mysql-boost`,
so the version-pinned Boost is downloaded once and reused. Use `release` for a
`RelWithDebInfo` build.

### What the script runs under the hood

```bash
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Debug \
  -DCMAKE_CXX_COMPILER_LAUNCHER=ccache \
  -DDOWNLOAD_BOOST=1 -DWITH_BOOST=~/.cache/mysql-boost \
  -DWITH_UNIT_TESTS=ON
cmake --build build -j"$(nproc)"
```

## Running tests

MySQL uses **MTR** (MySQL Test Run) under `mysql-test/`.

```bash
scripts/ci/mtr.sh                    # default MTR test selection — the PR check
scripts/ci/mtr.sh --suite=innodb     # any suite; args pass straight to ./mtr
```

Logs land in `build/mysql-test/var/log/`. The `MTR` PR workflow invokes the same
default MTR command, so a green local run predicts a green PR check.

## Troubleshooting

| Symptom                              | Fix                                                        |
|--------------------------------------|-----------------------------------------------------------|
| Boost version error at configure     | delete `~/.cache/mysql-boost` and re-run `build.sh`        |
| `bison: command not found`           | re-run `scripts/ci/bootstrap.sh`                          |
| Slow rebuilds                        | confirm ccache is on the PATH; `ccache -s` shows hit rate  |
