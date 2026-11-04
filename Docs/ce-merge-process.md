# Merge pipeline

A configured maintainer comments `/integrate` on an approved, OCA-verified PR.
The PR must target `8.4`, `9.7`, or `trunk`. Target the oldest applicable version;
the pipeline always continues through trunk.

One GitHub Actions run does three things:

1. **Prepare:** validate the maintainer and source review, then prepare local
   candidates for the source version and every later version. A conflict fails
   this job before any version branch changes.
2. **Test:** run every affected version in parallel using the ordinary shared
   `scripts/ci` commands: GCC/Clang debug builds, four MTR shards, unit tests,
   and formatting. All jobs must succeed. There are no demo shortcuts or CI
   reuse rules.
3. **Merge:** squash the source PR, then create and merge native PRs between
   version branches, preserving the lower version's actual commit in each
   higher version's history. Generated forward-merge PRs use the configured
   bot review exception; the source PR always needs human approval.

For a PR targeting `8.4`, the tests cover `8.4`, `9.7`, and `trunk`. A PR targeting
`9.7` tests `9.7` and `trunk`. A PR targeting `trunk` tests only trunk.
The source PR receives a link to the pipeline run; Actions shows progress and failures.

## No persistent coordinator

There is no state branch, background poller, persistent lock, operator menu,
resolution-PR protocol, or automatic retry loop. Actions serializes entire merge
pipeline runs with one repository concurrency group. The temporary candidate
bundle and test plan are artifacts of that run, retained for one day. They are
not committed or pushed to the repository. Each test runner imports the candidates
without receiving the merge credential.

Small Git checks remain necessary to test the proposed change on every version.
Testing the untouched branch tips would not validate the contribution. The merge
job also verifies that branch tips have not moved and that GitHub's actual merged
files match the tested files. If a target changes during testing, the pipeline
fails rather than merging stale results.

## Failure handling

A test failure or preparation conflict leaves all version branches unchanged.
Fix the source PR or conflicting branch, obtain current-head approval, then post
`/integrate` again. There are no special resolution commands or metadata fields.

Publication uses separate native GitHub PR merges, so it is not atomic across
branches. A protection change, conflict, or network failure during publication
can leave earlier versions merged, even though all tests passed beforehand.
The pipeline stops and never blindly retries an uncertain merge. Inspect the
actual PR states and finish any remaining forward merges through the normal
reviewed PR process before starting another integration. Re-running only the
merge job will reject already merged sources or moved targets.

## Configuration and cutover

`.github/ce-merge-policy.json` supplies the repository, ordered branch chain,
maintainers, OCA label, automation account ID, private-content restrictions,
and optional per-version MTR suite lists. The fixed job structure lives in
`.github/workflows/ce-merge.yml` and `merge-version-tests.yml`.

Keep `CE_MERGE_TOKEN` as the automation account's repository-scoped secret.
The account needs PR, contents, issues, and commit-status access required by the
existing native merge setup. Test jobs receive no secret. Preserve the existing
PR-only review exception for generated forward merges, required `Merge check`,
PR use, and history protections. The workflow does not provision or modify rulesets.
Every supported version must contain the trusted shared `scripts/ci` commands.

Before publishing this replacement, finish or reconcile any old coordinator
operation and disable its scheduled workflow. Deploy the reviewed replacement
on the default branch. An existing state branch can remain as historical data;
the replacement never reads or writes it. Rehearse the complete run on the fork
with full tests before production use. No remote deployment or ruleset changes
are performed by local validation.

Local regression checks:

```sh
python3 -W error::ResourceWarning -m unittest discover -s scripts/ce_merge/tests -v
```
