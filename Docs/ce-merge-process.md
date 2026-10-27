# Merge Bot: test across versions, then merge

The **Merge Bot** runs trusted default-branch automation on GitHub-hosted runners.
Maintainers request an operation with `/integrate`. The bot first prepares and tests
the complete version chain without opening generated integration PRs. It then
squashes the original PR and opens and merges one integration PR at a time using
the actual preceding merge commit. All publication PRs have GitHub's native
**Merged** state. Earlier merges remain published if a later merge is blocked.

The fork uses the real version branch chain `8.4` → `9.7` → `trunk`, with
`ci_mtr_test: main.1st` selecting the rehearsal test in each MTR shard. The rehearsal uses
`RidhaOracle` (GitHub ID `91072007`) as the automation account in `bot_user_id`.
Its token must be supplied as `CE_MERGE_TOKEN`. There is no shadow mode or separate
activation gate.

## Contributor interface

Target the oldest applicable configured version and include:

```text
Merge-Through: trunk
```

Optional version-specific conflict resolutions can be linked in the same body:

```text
Resolution-PR: 9.7 https://github.com/RidhaOracle/mysql-server/pull/456
Resolution-PR: trunk https://github.com/RidhaOracle/mysql-server/pull/457
```

Each configured later version can have one resolution link. Numbers such as
`#456` are accepted too; URLs must belong to this repository. Hidden examples in
HTML comments are ignored. When the PR target equals `Merge-Through`, the bot
infers **backport null merge**:
apply the fix only to that target, then merge its history through every later
version without changing their files. No separate reason field is required.
For example, a PR targeting `8.4` with `Merge-Through: 8.4` changes `8.4`, then
creates history-only merge PRs into `9.7` and `trunk`. A PR targeting `9.7` with
`Merge-Through: 9.7` changes `9.7`, null-merges into `trunk`, and leaves `8.4` alone.

For a content stop beyond the PR target but before the newest version, include
`Merge-Reason:` with a reviewed applicability explanation. Supplied reason fields
must be non-empty. Later versions receive history-only merges with unchanged
files, even when a normal content merge would conflict. A conflict within the
content-carrying part of the chain still needs a reviewed resolution PR. Backport
patches must be submitted to the desired older target; the bot does not create
backward cherry-picks from a PR targeting a newer version, and `Backport-To:`
instructions remain unsupported.

The original PR needs trusted OCA verification, current-head approval by a
reviewer with Write, Maintain, or Admin access, resolved conversations, and passing
ordinary PR CI. Wait for successful **Merge check**, then have a configured
maintainer with current Write, Maintain, or Admin access post a new `/integrate`
comment. Authorization binds its head, base branch, body, and deployment policy to
readiness published before the comment. Edited comments are ignored; duplicate
comment delivery reuses the operation. Commands in the same second as readiness
are rejected conservatively. The `Integrate` label is advisory.

## Phase 1: test changes across versions

1. Acquire the persistent merge lock, restore public Git objects and journal
   checkpoints, and verify that the initial lower-to-higher branch ancestry is in
   sync. No contributor code runs in the credentialed coordinator.
2. Prepare local squash and subsequent merge candidates. Pin each target
   SHA, resulting file tree, parent list, and any reviewed resolution. Rehearse
   the full chain before publishing anything.
3. For each version, inspect reusable **PR Build**, **MTR**, and applicable
   **Format Check** results on the original or its linked resolution PR. Reuse is
   per check group: run only groups with missing or stale evidence. An explicitly
   failed latest applicable run blocks; fix/rerun it rather than hiding the failure.
4. Dispatch **Merge Version Tests** on the trusted default branch for missing
   checks. Candidates come from a Git bundle in a pinned `ce-merge-state`
   checkpoint. No integration or testing branches are created. Separate read-only
   runners verify the checkpoint, bundle, exact candidate SHA, target ancestry,
   tree, and trusted version scripts before executing builds.
5. Wait until every version has passing evidence. Runs exit while waiting and
   resume from CI/PR events or the five-minute schedule.

Per version, normal coverage is GCC and Clang debug builds, four MTR shards
(replication, storage, core, services), CTest unit tests in the services shard,
and formatting for changed C/C++ files. Existing test retries and quarantines are
preserved. Build, MTR, unit, and formatting scripts are shared with ordinary CI.
Version scripts are pinned to the target baseline, not a contributor revision.
Optional `ci_suites` maps configured branch names to four shard suite lists for
versions with different suite availability. Missing suites fail explicitly.
Custom suite profiles require candidate MTR evidence rather than assuming ordinary
PR coverage is equivalent. Empty `ci_mtr_test` requires full coverage; `main.1st`
is an explicit bot rehearsal setting, not production validation.

The coordinator validates workflow identity, repository, PR/head/base association,
latest runs, job attempts, required steps, full or rehearsal coverage, and unit
test completion. Candidate runs additionally bind a deterministic request ID,
trusted workflow revision, bot actor, suite profile, and requested groups. Results
are read from GitHub run/job metadata; candidate artifacts are never executed or
trusted by the coordinator.

PR CI reuse requires the same target snapshot and resulting file tree. Ordinary
source readiness retains the existing accepted-earlier-target-ancestor policy for
an unchanged head with a clean current merge. Documentation exemptions are limited
to `Docs/`, `README`, `README.md`, and `CONTRIBUTING.md`. Required CI workflow edits
need a reviewed bootstrap onto trusted baselines before their evidence is accepted.

## Conflict resolution PRs

A resolution PR supplies the complete reviewed, resolved result for one version.
It must target the recorded version/base and contain both the exact lower
candidate and recorded target as ancestors. A similar patch or another PR fixing
the same bug is insufficient. The bot checks OCA, current-head approval,
conversations, content restrictions, and the pinned PR body and SHA.

When a merge conflicts, the progress comment gives a public state bundle filename,
exact input SHA and target SHA. Fetch the state branch and import the saved input:

```bash
git fetch origin ce-merge-state
git show FETCH_HEAD:journal/bundles/<bundle-name>.bundle > ce-merge.bundle
git fetch ./ce-merge.bundle 'refs/ce-candidates/*:refs/ce-candidates/*'
```

Merge the exact input and target in your resolution PR's existing branch,
resolve the files, and open/reuse a resolution PR. A metadata link is consumed
automatically; otherwise run **Use resolution PR** (`use-resolution`) with the
operation ID and PR number or URL. Attaching a late PR this way avoids editing the already-authorized source
body. The manual operation requires a current configured maintainer.

The bot takes the reviewed file tree, prepares successors again, and reuses the
resolution PR's matching CI where possible. Other versions still need their own
evidence. Changed resolution heads, bodies, bases, dismissed approvals, lost
reviewer access, and change requests block its use. A moved resolution target
requires a fresh resolution; it is never silently rebased.

The resolution PR itself is merged into its version during publication. It must
include the actual preceding version merge, not only the local preflight commit.
After the source squash creates that actual commit, update a preflight resolution
PR to include it and obtain fresh approval and CI. The bot never copies a reviewed
tree onto a new branch. No propagation PRs are opened during testing;
contributor-provided resolution PRs are the deliberate exception.

For a backport, a normal version-branch merge is permitted only when it preserves
the newer target tree exactly. Otherwise the bot pauses before publication for a
reviewed history-only resolution PR. It never substitutes a content-changing merge
for an ancestry-only request.

## Phase 2: merge changes across versions

After all tests pass:

1. Checkpoint an exact source merge intent and call
   `gh pr merge --squash --match-head-commit`. Read GitHub's merged state and verify
   the actual squash commit **F**, its parent, tree, and target reachability.
2. Prepare the next version from **actual F**, retaining any reviewed resolution
   files. If its target or file tree differs from testing, obtain fresh evidence
   before publication. Revalidate existing results, including failed reruns.
3. Open or reuse a PR directly between the actual version branches: **8.4 → 9.7**,
   then **9.7 → trunk**. Its head is the actual lower version commit. A conflicting
   hop uses the maintainer-provided resolution PR instead. No bot-created branches
   or extra integration commits are used. **Merge check** links revalidated
   testing evidence, not a claim that tests ran on a new SHA.
4. Verify the bot author, repository, exact source branch/head/base, expected body and
   tested file tree, and check for objections or unresolved discussions. The
   approved policy allows generated integration PRs without another human approval.
   The source and resolution PRs always retain their human-review requirements.
5. Call `gh pr merge --merge --match-head-commit`, verify the actual result **M9**,
   persist it, then repeat for trunk using actual M9. Each actual lower result
   remains an ancestor of the next. Release the lock only after the final result
   is confirmed.

Review waiver is enforced by the automation account's User exception in the **review-only**
ruleset, restricted to PR merges. Separate rules still require PR use, Merge check,
resolved conversations, no force pushes, and no deletion, without a bot bypass.
The update restriction also allows the bot only through PR merges. GitHub cannot
scope a User exception by generated PR authorship; the trusted coordinator enforces
that distinction and independently requires source/resolution approval.
With the current rehearsal account, this identity exception also applies to
manual actions by `RidhaOracle`; it is not restricted to Actions runs. PRs,
comments, and statuses produced by automation appear under that account. Source
and resolution PRs authored by `RidhaOracle` need another collaborator's approval.

Native merges use the CLI's `--admin` switch to suppress its local refusal when
the update or review restriction reports a PR as blocked. GitHub still evaluates
the account's configured permissions. The coordinator independently requires
current-head human approval for every source and resolution, including retries;
only verified generated PRs waive separate approval. Quality and history rules
have no bot exception. Extra organization rules and required checks are never
presumed bypassable. See GitHub's
[ruleset actor and PR-only bypass API](https://docs.github.com/en/rest/repos/rules#create-a-repository-ruleset).

Content-based tests may be reused across different merge SHAs only after matching
target, files, policy, and trusted testing definitions. Tests that intentionally
inspect history or commit identity must run on the actual candidate; the
`refresh-checks` operation forces fresh evidence instead of PR reuse. A target
race or unexpected merged tree stops propagation for investigation.

## State and recovery

Protected `ce-merge-state` holds operation manifests, the soft lock, audit history,
and candidate bundles. Each runner restores authoritative state. Public Git object
caches are optional. Expected-head checkpoints precede every remote mutation;
failed persistence or competing writers stop all further writes.

All events share one repository-wide Actions concurrency group with cancellation
disabled. The persistent soft lock spans runs, testing waits, and partial merges;
it has no timeout expiry. Each reconciliation pass attempts at most one merge.
Do not cancel a run that may have submitted a merge or manually update a locked
branch. A lost CLI response is reconciled against the same PR, never blindly
retried. Test dispatch intents are also checkpointed. If a dispatch response was
lost and no matching run appears, `refresh-checks` explicitly starts a new attempt.

Manual commands require a configured maintainer and the default workflow branch:

| Command | Purpose |
| --- | --- |
| `once` | Reconcile/advance one bounded pass |
| `status`, `audit` | Inspect operations, lock, holds, or audit history |
| `snapshot` | Verify the completed chain; refuses active/uncertain work |
| `use-resolution` | Supply `operation` and resolution PR number/link in `pr` |
| `refresh-checks` | Force new candidate testing for remaining versions |
| `resume` | Retry blocked work with the same authorized operation |
| `abort` | Release an unpublished operation; refuses partial or uncertain merges |
| `reconcile` | Confirm the recorded in-flight PR merge |
| `retry` | Reconcile first, then revalidate and retry the same open PR |
| `hold`, `unhold` | Record public reason in `record`; refuse uncertain publication |
| `init-state` | Explicit first initialization |
| `rulesets` | Print administrator-applied rulesets/settings |
| `verify-deployment`, `attest-deployment` | Verify protections/full administrator export |

An abort cannot undo earlier merges. Resolve the blocked version and resume
propagation. Uncertain operations retain the lock until reconciled. Do not delete
state or force-push to clear it. Reporting failures cannot repeat confirmed merges.

## Provision and cut over

1. Stop/fence the old service. Reconcile outstanding merges and finish or abort
   unpublished operations using their original coordinator. Automatic journal
   migration is excluded; unfinished older protocols are refused.
2. The current rehearsal uses the existing repository owner `RidhaOracle`; no
   separate account is needed. While signed into that account, create a
   fine-grained personal access token with resource owner `RidhaOracle`, limited
   to `mysql-server`, and an expiration. Grant read/write access to Contents,
   Pull requests, Issues, Commit statuses, and Actions (needed to dispatch tests);
   Workflows write and Administration read. Store it as the repository Actions secret
   `CE_MERGE_TOKEN`. The coordinator verifies its account against `bot_user_id`.
   If switching identities later, note that GitHub currently excludes outside
   and repository collaborators from fine-grained PAT support; organization
   members and repository owners can use this setup. See
   [GitHub token limitations](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#fine-grained-personal-access-tokens-limitations).
3. Publish reviewed automation and the dispatch workflow on the default branch.
   Bootstrap the trusted shared CI scripts and matching ordinary CI workflows onto
   every supported target baseline. Existing inline CI remains until this reviewed
   bootstrap; new wrappers need those baseline scripts. Confirm each version's
   suite profile and full/rehearsal setting.
4. Generate `python3 -m scripts.ce_merge rulesets`, then apply the reviewed output
   as an administrator. Enable squash/merge commits; disable rebase and auto-merge.
   Apply the new PR-only bot review exception, mandatory PR/quality/history rules,
   and the protected persistent state branch. No temporary branch namespace is
   provisioned. Remove obsolete
   App requirements/receipt rules. Preserve immutable tags. **Merge check** is the
   aggregate required check; requiring raw PR Build/MTR checks as well would prevent
   per-version evidence reuse. Never give the bot a quality/history bypass.
5. Run `init-state` and `verify-deployment`. If GitHub hides bypass actors, commit
   a reviewed full administrator export to `.github/ce-merge-rulesets.json`, with
   `{"repository":"OWNER/REPO","rulesets":[FULL_RULESET_OBJECTS]}`. Run
   `attest-deployment`, then verify again. Policy, IDs, source, and update times bind
   the attestation. Changes require a new export.
6. Rehearse on the configured test chain: standalone CI with no generated PRs,
   per-check resolution CI reuse, fresh-runner restoration, source/resolution review
   revocation, generated merges without extra approval, CI/history rejection even
   with the review exception, conflicts, lost responses, and partial propagation.
   Verify actual ancestry and native **Merged** states. Live GitHub behavior and
   organization rules require this rehearsal before switching to production/full MTR.
7. Remove obsolete App credentials/installations/webhooks after reconciliation.

Local validation uses disposable Git repositories and simulated GitHub responses:

```sh
python3 -W error::ResourceWarning -m unittest discover -s scripts/ce_merge/tests -v
```
