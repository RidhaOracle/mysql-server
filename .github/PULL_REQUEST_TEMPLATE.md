<!-- # Copyright (c) 2026, Oracle and/or its affiliates. -->
<!-- Thanks for contributing to MySQL Server! The prompts below are the few
     things reviewers always need. Delete any that don't apply. -->

### What does this change do?

<!-- One or two sentences. Link the issue/bug it fixes: "Fixes #1234" or
     "BUG#XXXXXXXX". -->

### Why is it needed?

### LTS propagation

<!-- In GitHub's base dropdown, select the OLDEST applicable supported branch.
     Example: for a fix needed in 8.4 and trunk, base = 8.4 (if configured).
     The bot opens PRs to carry the fix forward; all targets publish together
     after every PR is reviewed and tested. Do not open against trunk for backporting.

     Usually leave the fields below commented: propagation defaults to Innovation.
     To stop CONTENT earlier, uncomment both lines, use an actual configured branch,
     and explain why newer branches do not need the fix. Reviewed ancestry-only
     merges still continue through Innovation. The fields do not select the PR base.
Upmerge-Till: <last-branch-needing-content>
Upmerge-Reason: <why-newer-branches-do-not-need-the-fix>
-->

### How was it tested?

- [ ] Added/updated MTR tests under `mysql-test/`
- [ ] `scripts/ci/mtr.sh` passes locally
- [ ] Ran the relevant full suite (name it): ______

### Contributor checklist

- [ ] Code is formatted (`scripts/ci/format.sh`)
- [ ] Commits are focused with descriptive messages

### AI assistance

- [ ] I did not use AI assistance for this contribution
- [ ] I used AI assistance for this contribution

If AI assistance was used, describe the tool(s) and extent of use:

<!-- e.g. brainstorming, code generation, test generation, refactoring, review.
     Also mention which parts were human-reviewed or manually verified. -->

### Areas touched

<!-- e.g. innodb, optimizer, replication, client, build. Helps auto-routing. -->
