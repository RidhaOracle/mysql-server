# Mark Integrate Workflow

## Goal

Maintain an `integrate` pull-request label that reflects whether at least one reviewer currently has an active approval.

## Workflow

Create `.github/workflows/mark-integrate.yml` with these characteristics:

- Run for `pull_request_review` events of type `submitted` and `dismissed`.
- Grant read-only contents access and write access for issues and pull requests.
- Serialize runs by pull-request number so simultaneous review events cannot leave a stale label.
- Use `actions/github-script@v7` on `ubuntu-24.04`.
- Ensure the `integrate` label exists with color `5319E7` and description `Approved patch ready for integration`.

## Review-State Reconciliation

Fetch all reviews for the pull request with pagination. For each reviewer, retain their latest meaningful state in API order:

- `APPROVED`, `CHANGES_REQUESTED`, and `DISMISSED` replace that reviewer's previous meaningful state.
- `COMMENTED` and `PENDING` do not change the reviewer's approval state.

If any reviewer's latest meaningful state is `APPROVED`, add the `integrate` label. Otherwise, remove the label if present. Treat a missing label during removal as an already-correct state; propagate all other API errors.

## Operational Notes

The workflow does not check out or execute pull-request code. Like other event-driven GitHub Actions workflows, it starts operating after it is present on the repository's base/default branch.

## Verification

- Parse all GitHub Actions YAML files.
- Confirm the workflow trigger, permissions, concurrency group, runner, and action version.
- Syntax-check the embedded JavaScript.
- Exercise review-state scenarios covering approval, comments after approval, changes requested after approval, dismissed approval, and multiple reviewers.
- Check the final diff and preserve the user's pre-existing untracked files.
