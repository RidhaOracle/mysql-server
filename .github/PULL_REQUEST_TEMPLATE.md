<!-- # Copyright (c) 2026, Oracle and/or its affiliates. -->
<!-- Thanks for contributing to MySQL Server! The prompts below are the few
     things reviewers always need. Delete any that don't apply. -->

### What does this change do?

<!-- One or two sentences. Link the issue/bug it fixes: "Fixes #1234" or
     "BUG#XXXXXXXX". -->

### Why is it needed?

### Merge across versions

<!-- Target the OLDEST applicable configured version. The bot tests candidates
     across all versions first, then squashes this PR and creates/merges forward
     integration PRs one at a time. Source and resolution PRs need human review;
     verified generated PRs do not need an additional approval.

     Replace Merge-Through below with the last version needing the fix. For an
     earlier stop, add Merge-Reason explaining why newer versions need no content.
     History-only merges still continue through the last configured version.

     Optional reviewed conflict resolutions (one per target version):
     Resolution-PR: codex/ce-test-lts-new https://github.com/RidhaOracle/mysql-server/pull/456
     Resolution-PR: codex/ce-test-innovation https://github.com/RidhaOracle/mysql-server/pull/457
     Resolutions must include the exact candidate and target inputs published by
     the bot. Matching CI is reused; missing tests run independently of PR creation.
     Earlier merges remain if a later version blocks. No backports from trunk. -->

Merge-Through: codex/ce-test-innovation

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
