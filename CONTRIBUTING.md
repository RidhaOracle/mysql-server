# Contributing to MySQL Server

We welcome your code contributions. This guide gets you from a fresh clone to a
merged pull request with as little friction as possible.

> **TL;DR** — Sign the [OCA](https://oca.opensource.oracle.com), run
> `scripts/ci/bootstrap.sh`, make your change, run `scripts/ci/mtr.sh`,
> and open a PR. CI builds your branch and runs MTR automatically. A
> maintainer is assigned within the triage SLA below.

---

## 1. Sign the Oracle Contributor Agreement (once)

Before any contribution can be merged you must have signed the
[Oracle Contributor Agreement (OCA)](https://oca.opensource.oracle.com).

1. Create or reuse a user account at <https://bugs.mysql.com>.
2. Sign the OCA, referencing that account.
3. Use the **same email** on your Git commits (`git config user.email`).

Oracle verifies OCA status separately from this repository's GitHub Actions
workflows. Follow any OCA guidance reported on the pull request before the
change is merged.

## 2. Get a build in one command

Full instructions live in [`docs/development/BUILD-FROM-SOURCE.md`](docs/development/BUILD-FROM-SOURCE.md).
The fast path:

```bash
# Reproducible toolchain + Boost, identical to CI:
scripts/ci/bootstrap.sh        # installs/pins deps
scripts/ci/build.sh debug      # configures + builds into build/
```

These scripts pin the same compiler, CMake, Ninja, Boost, and test tooling used
by CI, so "works locally" tracks "passes in CI."

## 3. Find something to work on

- Issues labeled [`good first issue`](../../labels/good%20first%20issue) are
  scoped, have reproduction steps, and a named area owner.
- [`help wanted`](../../labels/help%20wanted) marks larger items the team would
  welcome help on.
- For a substantial change (new syntax, on-disk format, public behavior), open a
  short **RFC** first — see [`docs/rfcs/`](docs/rfcs/). This avoids investing in a
  branch that conflicts with internal direction, the single most common reason
  external work has historically stalled.

## 4. Make the change

- Match existing style; formatting is enforced by `.clang-format`. Run
  `scripts/ci/format.sh` (or install the pre-commit hook below) so you never get
  a review comment about whitespace.
- Add or update tests. Every behavior change ships with MTR coverage under
  `mysql-test/`. See `docs/development/BUILD-FROM-SOURCE.md#running-tests`.
- Keep commits focused and write a clear message body explaining *why*.

Optional but recommended — install the format pre-commit hook:

```bash
ln -s ../../scripts/ci/format.sh .git/hooks/pre-commit
```

## 5. Run tests locally (the same ones CI runs)

```bash
scripts/ci/mtr.sh            # default MTR test selection, mirrors the PR check
scripts/ci/mtr.sh --suite=innodb     # pass MTR args straight through
```

## 6. Open the pull request

Push your branch and open a PR against `trunk`. The
[pull request template](.github/PULL_REQUEST_TEMPLATE.md) prompts for the few
things reviewers always need. On open, CI automatically:

- checks formatting,
- builds Debug on gcc and clang,
- runs MTR,
- auto-labels the affected area and assigns a reviewer.

CI reports completion or actionable failures after the build and MTR run finish.

## What to expect from us (triage SLAs)

These are the response targets the maintainers hold themselves to. They are
published here so the contract is mutual and visible:

| Stage                                   | Target            |
|-----------------------------------------|-------------------|
| First maintainer response on a new PR   | 3 business days   |
| First response on a `good first issue`  | 2 business days   |
| Review round-trip after you push        | 5 business days   |
| Decision on an RFC                       | 15 business days  |

If a PR goes quiet past these windows, ping `@mysql/triage` on the thread.

## Alternative submission path

You may still attach a patch to a bug record at <https://bugs.mysql.com> via the
*contribution* tab. GitHub pull requests are now the recommended path because
they get automated CI feedback and public review history.
