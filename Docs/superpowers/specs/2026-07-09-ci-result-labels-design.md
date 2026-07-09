# CI Result Labels

## Goal

Expose the latest definitive PR Build and MTR Smoke outcomes as mutually exclusive pull-request labels.

## Labels

The workflows manage four title-cased labels:

| Workflow | Success label | Failure label |
|---|---|---|
| PR Build | `Build Passed` | `Build Failed` |
| MTR Smoke | `MTR Passed` | `MTR Failed` |

Passed labels use color `0E8A16`. Failed labels use color `D93F0B`. Descriptions identify whether the corresponding PR build or smoke suite passed or failed.

## Workflow Design

Modify `.github/workflows/pr-build.yml` and `.github/workflows/mtr-smoke.yml`. Keep their current triggers, path filters, concurrency, caches, build/test commands, timeouts, artifacts, and matrix settings unchanged.

Add repository permissions for read-only contents and issue-label writes. Add one reporter job to each workflow:

- The build reporter depends on the complete `build` matrix so one compiler leg cannot race another.
- The MTR reporter depends on the `smoke` job.
- Each reporter runs on `ubuntu-24.04` with `always() && !cancelled()`.
- Success selects the Passed label; any non-canceled failure or timeout selects the Failed label.
- The reporter ensures both label definitions exist with the expected metadata, removes the opposite label if present, and adds the selected label.
- A missing opposite label during removal is an already-correct state; all other API errors fail the reporter.

Canceled superseded or manually canceled workflows do not write a false failure label. The next definitive completed run replaces the previous Passed/Failed label.

## Verification

- Parse all GitHub Actions YAML files.
- Confirm reporter dependencies, aggregate result expressions, permissions, and cancellation guards.
- Syntax-check both embedded JavaScript blocks.
- Exercise mocked success and failure outcomes for each workflow, verifying that the opposite label is removed before the selected label is added.
- Inspect the final diff and preserve the user's existing untracked files.
