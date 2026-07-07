# Remove Repository OCA Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the repository-owned OCA checker while preserving OCA policy text and assigning unmatched files to the two new default owners.

**Architecture:** Delete the self-contained OCA workflow and its fallback data, then correct the two documents that describe that automation. Update only the CODEOWNERS catch-all so existing area-specific routing remains intact.

**Tech Stack:** GitHub Actions YAML, CODEOWNERS syntax, Markdown, Ruby YAML parser, shell assertions

---

### Task 1: Remove the repository-owned OCA automation

**Files:**
- Delete: `.github/workflows/oca-check.yml`
- Delete: `.github/oca-allowlist.txt`
- Modify: `CONTRIBUTING.md:22-25,79-84`
- Modify: `STREAMLINING-CHANGESET.md:32,53-55`
- Modify: `.github/PULL_REQUEST_TEMPLATE.md:19-20`
- Modify: `.github/workflows/pr-template-check.yml:39-40`

- [ ] **Step 1: Record the failing pre-change audit**

Run:

```bash
test ! -e .github/workflows/oca-check.yml && test ! -e .github/oca-allowlist.txt
```

Expected: exit status `1` because both obsolete files still exist.

- [ ] **Step 2: Delete the workflow and fallback fixture**

Delete `.github/workflows/oca-check.yml` and `.github/oca-allowlist.txt` in full.

- [ ] **Step 3: Correct the contribution guide**

Replace the paragraph that describes the local `oca-check` workflow with:

```markdown
Oracle verifies OCA status separately from this repository's GitHub Actions
workflows. Follow any OCA guidance reported on the pull request before the
change is merged.
```

In the list of automatic CI actions, remove `verifies your OCA status` while retaining formatting, build, test, labeling, and reviewer actions. Remove this checklist line from both the pull request template and the validator's expected template:

```markdown
- [ ] I confirm the code being submitted is offered under the terms of the OCA, and that I am authorized to contribute it.
```

Keep the preceding signed-OCA checkbox in both files.

- [ ] **Step 4: Correct the changeset summary**

Remove the `.github/workflows/oca-check.yml` row from Horizon 2. Replace the final obsolete fixture paragraph with:

```markdown
The `.clang-format` already in the tree is reused as-is; nothing here redefines
code style. OCA verification is handled separately by Oracle rather than by a
workflow in this repository.
```

- [ ] **Step 5: Verify the automation is gone and policy remains**

Run:

```bash
test ! -e .github/workflows/oca-check.yml
test ! -e .github/oca-allowlist.txt
git grep -n -E 'oca-check|oca-allowlist|oca:verified|oca:unsigned' -- . ':!Docs/superpowers'
git grep -n 'I have signed the \[OCA\]' -- .github/PULL_REQUEST_TEMPLATE.md .github/workflows/pr-template-check.yml
! git grep -n 'I confirm the code being submitted is offered under the terms of the OCA' -- .github/PULL_REQUEST_TEMPLATE.md .github/workflows/pr-template-check.yml
git grep -n 'Oracle Contributor Agreement' -- CONTRIBUTING.md
```

Expected: both `test` commands pass; the obsolete-reference and removed-attestation searches return no matches; the positive searches show the retained signed-OCA requirement.

- [ ] **Step 6: Commit the automation removal**

```bash
git add .github/workflows/oca-check.yml .github/oca-allowlist.txt .github/PULL_REQUEST_TEMPLATE.md .github/workflows/pr-template-check.yml CONTRIBUTING.md STREAMLINING-CHANGESET.md
git commit -m "Remove repository OCA checker"
```

### Task 2: Change the default code owners

**Files:**
- Modify: `.github/CODEOWNERS:4-10`

- [ ] **Step 1: Record the failing owner assertion**

Run:

```bash
grep -qxF '*                          @seemasundara @gopshank' .github/CODEOWNERS
```

Expected: exit status `1` because the catch-all still names `@RidhaOracle`.

- [ ] **Step 2: Update the catch-all and explanatory comment**

Set the opening comment and default entry to:

```text
# Unmatched paths use the default owners below. Existing area-specific paths
# continue to use their explicitly listed owner.

# Default owners for everything not matched more specifically:
*                          @seemasundara @gopshank
```

Keep every area-specific `@RidhaOracle` line unchanged.

- [ ] **Step 3: Verify default and specific ownership**

Run:

```bash
grep -qxF '*                          @seemasundara @gopshank' .github/CODEOWNERS
test "$(grep -c '^[^#].*@RidhaOracle' .github/CODEOWNERS)" -eq 9
```

Expected: both assertions pass; the catch-all has two owners and all nine area-specific entries remain.

- [ ] **Step 4: Commit the owner update**

```bash
git add .github/CODEOWNERS
git commit -m "Update default code owners"
```

### Task 3: Validate the complete change

**Files:**
- Verify: `.github/workflows/*.yml`
- Verify: the seven implementation files changed since pre-implementation scope commit `e79588df945`

- [ ] **Step 1: Parse every remaining workflow**

Run:

```bash
ruby -e 'require "yaml"; Dir[".github/workflows/*.{yml,yaml}"].each { |file| YAML.load_file(file); puts file }'
```

Expected: every remaining workflow path is printed and Ruby exits with status `0`.

- [ ] **Step 2: Check whitespace and review the diff**

Run:

```bash
git diff --check e79588df945..HEAD -- .github/CODEOWNERS .github/PULL_REQUEST_TEMPLATE.md .github/workflows/pr-template-check.yml .github/workflows/oca-check.yml .github/oca-allowlist.txt CONTRIBUTING.md STREAMLINING-CHANGESET.md
git diff --stat e79588df945..HEAD -- .github/CODEOWNERS .github/PULL_REQUEST_TEMPLATE.md .github/workflows/pr-template-check.yml .github/workflows/oca-check.yml .github/oca-allowlist.txt CONTRIBUTING.md STREAMLINING-CHANGESET.md
git diff e79588df945..HEAD -- .github/CODEOWNERS .github/PULL_REQUEST_TEMPLATE.md .github/workflows/pr-template-check.yml .github/workflows/oca-check.yml .github/oca-allowlist.txt CONTRIBUTING.md STREAMLINING-CHANGESET.md
git status --short
```

Expected: no whitespace errors; the diff is limited to the seven requested files; only the user's pre-existing untracked `.DS_Store` files remain in status.
