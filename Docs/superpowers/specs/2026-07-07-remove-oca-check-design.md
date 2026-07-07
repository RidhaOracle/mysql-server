# Remove Repository OCA Check

## Goal

Remove the repository-owned Oracle Contributor Agreement (OCA) automation because a separate Oracle service now performs that verification. Keep the contributor-facing OCA policy and set the default code owners to `@seemasundara` and `@gopshank`.

## Changes

- Delete `.github/workflows/oca-check.yml`, including its status, label, and comment automation.
- Delete `.github/oca-allowlist.txt`, which exists only as the deleted workflow's fallback data.
- Keep the OCA requirements in `CONTRIBUTING.md`, `.github/PULL_REQUEST_TEMPLATE.md`, and `.github/workflows/pr-template-check.yml`.
- Remove statements in `CONTRIBUTING.md` that claim this repository verifies or labels OCA status. State instead that Oracle performs the check separately.
- Remove the obsolete OCA workflow and allowlist entries from `STREAMLINING-CHANGESET.md`.
- Change only the catch-all entry in `.github/CODEOWNERS` to `@seemasundara @gopshank` and update its surrounding comment. Leave path-specific `@RidhaOracle` entries unchanged.

## Resulting Behavior

Pull requests continue to ask contributors to confirm the OCA requirements. The repository no longer runs an OCA workflow or stores a fallback allowlist. Files without a more-specific CODEOWNERS match request review from both new default owners; existing area-specific paths continue to request review from `@RidhaOracle`.

## Verification

- Confirm the two OCA automation files are absent.
- Search tracked files for references to `oca-check`, `oca-allowlist`, and OCA status labels; no active automation references should remain.
- Confirm contributor-facing OCA requirements remain in the PR template and contribution guide.
- Parse all remaining GitHub Actions YAML files.
- Inspect the final diff to ensure unrelated files and the user's untracked `.DS_Store` files are untouched.
