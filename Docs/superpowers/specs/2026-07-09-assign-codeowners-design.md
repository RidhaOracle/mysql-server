# Assign Code Owners Workflow

## Goal

Request review from the temporary default owners, `@seemasundara` and `@gopshank`, only after Oracle's external check has added the `OCA Verified` label.

## CODEOWNERS Scope

For now, the two default owners apply to every path. Simplify `.github/CODEOWNERS` to its wildcard rule and remove the path-specific `@RidhaOracle` overrides so native GitHub ownership matches the workflow's behavior.

## Workflow

Create `.github/workflows/assign-codeowners.yml` with these behaviors:

- Run on pull-request `opened`, `synchronize`, `reopened`, `ready_for_review`, and `labeled` events.
- Skip draft pull requests and any pull request without the exact `OCA Verified` label.
- Grant read-only contents access plus write access for pull requests and issues.
- Serialize runs by pull-request number.
- Fetch `.github/CODEOWNERS` through the Contents API from `devex/streamline` for the current PR test.
- Place a TODO beside that source ref requiring it to be changed to `trunk` before merge.
- Parse only the wildcard `*` rule and treat its `@...` entries as GitHub user reviewers.
- Exclude the pull-request author and users who are already requested.
- Request all remaining eligible reviewers in one API call.

If the CODEOWNERS file is unavailable, is not a regular file, lacks a wildcard rule, or has no eligible configured reviewers, fail with a clear error rather than silently claiming success.

## Review-Requested Label

After the reviewer request succeeds, or when the configured owners are already requested, ensure and add this label:

- Name: `Review Requested`
- Color: `1D76DB`
- Description: `Review requested from code owners`

Do not add the label when reviewer assignment fails. The label remains as evidence that the review request was made; approval-state transitions continue to be handled by the separate Mark Integrate workflow.

## Verification

- Parse all GitHub Actions YAML files and syntax-check the embedded JavaScript.
- Verify the event list, permissions, concurrency, exact OCA gate, test source ref, and TODO.
- Exercise mocked API scenarios for wildcard parsing, author filtering, already-requested filtering, successful assignment, and label ordering.
- Confirm CODEOWNERS contains only the two wildcard owners.
- Inspect the final diff and preserve the user's existing untracked files.
