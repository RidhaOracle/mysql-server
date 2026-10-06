# CE forward integration: contributor guide and operator runbook

Option A is implemented: start at the oldest applicable supported branch and
propagate toward Innovation. **All affected CE branch refs and an integration
receipt publish in one `git push --atomic`. There is no SQLite database.**
The checked-in configuration is **shadow mode**, with only `trunk` configured;
production LTS names, ownership, App IDs, and trusted CI ref still require
an administrator's inventory and non-production rehearsal.

## What the contributor and bot do

The PR's GitHub **base** dropdown selects the starting branch. `Upmerge-Till`
selects the last branch that needs the content, not the original PR target.
For example, if the deployed chain is `8.4 → 9.7 → trunk`, a fix needed in all
three starts with a PR whose base is `8.4`. The source branch can be in a personal
fork or, for contributors with push access, in the CE repository. Both follow
the same OCA, review, content, CI, and authorization gates. The contributor does
not first merge into `trunk`. These version names are illustrative only.

1. Contributor opens the original PR against the oldest applicable branch.
2. Trusted OCA verification, public CI, resolved conversations, and
   current-head approval from a reviewer with the repository Write, Maintain, or Admin
   role (including collaborators on personal repositories) make it eligible. CODEOWNERS is not an integration gate.
3. An authorized maintainer clicks **Integrate** on the App's `CE / policy` check.
   Authorization binds the PR head, target, and body. The label is advisory.
4. The App snapshots the chain and prepares the original squash commit and every
   forward merge commit. Nothing has merged into any CE target yet.
5. The App stages candidates in its public bot fork and opens an upmerge PR for
   each newer branch. Reviewers approve those exact candidates, including null
   merges. CI builds/tests the actual candidate commits that will be published.
6. After every target passes, the App rechecks authorization, reviews, checks,
   branch snapshots, release holds, and deployment policy. One atomic push updates
   all affected CE refs and creates an immutable receipt.
7. `CE / operation` reports the PRs, resulting SHAs, and receipt. Downstream SEC/EE
   synchronization follows independently from a recorded completed CE snapshot.

```mermaid
flowchart TD
    PR["Contributor PR: oldest applicable base"] --> Gate["OCA + current review + public CI"]
    Gate --> Action["Maintainer: Integrate exact revision"]
    Action --> Prepare["Bot prepares squash + complete forward chain"]
    Prepare --> Conflict{"Conflict?"}
    Conflict -- Yes --> Resolve["Resolution PR: contributor resolves; owner reviews"]
    Resolve --> Prepare
    Conflict -- No --> Review["Bot upmerge PRs: review + CI on every exact candidate"]
    Review --> Ready{"All gates pass and snapshots unchanged?"}
    Ready -- No --> Wait["Wait or rebuild; CE refs unchanged"]
    Wait --> Review
    Ready -- Yes --> Push["ONE atomic push: all CE targets + receipt"]
    Push --> Done["All targets published, or none"]
```

For ordinary forward propagation, omit `Upmerge-Till` (defaults to Innovation),
or put an uncommented line in the PR body:

```text
Upmerge-Till: trunk
```

An earlier content stop needs an explicit explanation, for example:

```text
Upmerge-Till: 8.4
Upmerge-Reason: The affected subsystem was removed from all newer supported branches.
```

That example still prepares and reviews ancestry-only merges into `9.7` and
`trunk`. Each keeps its target tree unchanged while retaining its lower branch as
an ancestor. A conflict is never treated as evidence that a fix is unnecessary.
The App ignores HTML-commented template examples and rejects backward targets,
empty/duplicate targets, and backport metadata. Every generated PR is reviewed;
the source PR's OCA eligibility carries forward. A contributor-supplied resolution
PR also needs trusted OCA verification.

Original contributions squash, retaining the source head's author and co-author
trailers for the introduced commits. Forward upmerges use merge commits. Security
promotion retains the individually approved SC commits. Rebase merging is disabled.
Because publication uses Git directly, GitHub PR UI updates are not transactional:
a squash-integrated original PR is closed with a successful `CE / operation`
check and receipt, rather than promising GitHub's native “Merged” flag. Upmerge
PR heads become reachable and GitHub can recognize those as merged. Notifications
and PR closure retry independently and cannot change a successful Git transaction.

## Atomicity, persistence, and enforcement

Git's [atomic push](https://git-scm.com/docs/git-push#Documentation/git-push.txt---atomic)
updates all requested refs or rejects all of them; unsupported servers fail closed.
Every target uses an exact expected-old-SHA lease and must advance by ancestry.
The App never falls back to sequential merge API calls or individual pushes.
The receipt tag `ce-integration/<operation-id>` is created in the same transaction.
This guarantee covers refs in the CE repository, not review events, PR UI changes,
bot-fork staging, or separate SEC and EE repositories.

A single service host uses a process-scoped `flock` to exclude concurrent workers.
There is no durable merge-lock owner or timeout-based unlock. Local JSON manifests
retain the authorized revision, prepared SHAs, fork PRs, CI dispatch identity, and
publication intent; Git bundles preserve prepared objects across restarts. These
are fsynced on persistent local disk, with append-only JSONL audit events. They
are needed to resume the exact approved batch, not to simulate multi-branch
atomicity. Do not put the journal on NFS, ephemeral Actions storage, or independent
replicas. Back up the journal and bundles together with the service stopped;
export audit events to access-controlled append-only storage. The local files
are not tamperproof against the service/filesystem administrator.

`rulesets` generates administrator-reviewed controls:

| Control | Enforcement |
| --- | --- |
| CE updates | Only the merge App may update configured CE target branch refs |
| PR review | Required PR, one Write/Maintain/Admin approval enforced by the App, stale-review dismissal, last-push approval, resolved threads |
| App exception | The App bypasses the native PR-only update rule solely to publish the reviewed batch via Git; its code enforces the PR review checks |
| Required checks | `CE / public-ci` and `CE / policy`, bound to this App, with **no bypass** |
| History/lifecycle | No force updates, deletion, or routine creation of configured CE target branches; no linear-history rule |
| Tags | Immutable; only the merge App creates receipts, separate release App creates release tags |

Branch rules cover the configured development-chain and release targets. Other
branches in the CE repository can be created and updated according to normal
repository permissions and any additional organization rules. The bot still uses
its separate controlled fork for staging; security promotion also retains that
controlled-fork requirement.

An App direct push is therefore part of this design. A requirement that even the
App may only use GitHub's single-PR merge endpoint would conflict with atomic
multi-branch publication. The App has read-only administration permission and
cannot alter its rulesets. Rehearse the combined native checks and narrowly scoped
PR-rule bypass on GitHub before activation. Existing organization rules can impose
additional restrictions; any rejection leaves the whole transaction unpublished.

The App checks every introduced commit's paths and metadata, not just the final
diff. Populate the actual EE/Cloud/private path denylist during inventory; the
checked-in `internal` and `internal-sec` entries are only the starting policy.
Human review remains necessary for confidential content not identifiable by paths.
Security work must remain private until authorized release, including forks/logs.

Every MTR shard checks that its configured suites exist in the candidate's public
source before installing the toolchain or building. CE validation uses the same
public suite lists as the ordinary MTR workflow. Missing suites fail explicitly;
no missing suite is silently skipped.

The bot does not exclude a review because its author also authored the PR. GitHub
itself prevents authors from submitting an Approve review on their own PR; removing
the bot's redundant check does not enable that native action or add a bot approval
action. Existing native review protections still apply.

## Install and cut over

Prerequisites: Python 3.10+, Git 2.38+ with `merge-tree --write-tree`, OpenSSL,
persistent local disk, outbound GitHub HTTPS, and TLS webhook ingress. The Python
service has no third-party dependencies. Never run contributor code on this host.

1. Inventory the production branch chain and repository reviewer roles. Keep existing public
   history and reconcile lower-to-higher ancestry through reviewed baseline work.
   Initialize SEC from CE and reconcile EE's CE baseline without replacing its
   tree. Routine sync refuses unrelated histories and does not resolve conflicts
   with automatic ours/theirs strategies.
2. Register the GitHub App from `.github/ce-merge-app.json` with the actual webhook
   URL. Install only on public CE and its controlled public bot fork. No human
   developers should push to that fork. `workflows: write` allows reviewed workflow
   changes to integrate; the App has no SEC/EE access. Use separate private and
   release credentials. Require organization 2FA for members/collaborators.
3. Copy `.github/ce-merge-policy.json` to `/etc/mysql-ce-merge/policy.json`. Set real
   repository/fork names, ordered branches (Innovation last), release branches,
   maintainers, App/installation IDs, and public-content policy. Keep `strategy`
   set to `forward`. Reviewers need the repository Write, Maintain, or Admin role.
   Collaborators on personal repositories qualify with Write access. Approval does not require membership
   in the configured `maintainers` allowlist, which controls integration requests
   and trusted OCA verification. CODEOWNERS may still be used for review routing.
4. Publish trusted automation and set `ci_ref` to a protected branch or tag, such
   as `trunk`. The App resolves it to a commit and records that revision before
   each dispatch; there is no manually maintained `ci_revision` setting. Legacy
   copies of that setting are ignored. The workflow must exist on the default
   branch for dispatch. Verify the build/test scripts against every supported branch.
5. Supply `CE_APP_PRIVATE_KEY` (PEM path) and `CE_WEBHOOK_SECRET` (32+ random
   characters) outside the checkout. The CLI defaults to a persistent public Git
   cache at `<state-dir>/public.git` and logs its location when starting `serve`
   or `once`. The first fetch may take several minutes; later evaluations reuse
   the downloaded objects. Set `CE_GIT_CACHE` to override this with an absolute
   path to a dedicated private directory containing only public CE Git objects.
   An unset or empty value uses the default. Do not share a cache across separate
   coordinator state directories or repositories. The packaged service explicitly
   uses `/var/lib/mysql-ce-merge/public.git`.
6. Generate ruleset payloads and apply them as an administrator in rehearsal.
   Provision branches before enabling lifecycle protection. Validate existing
   bypasses and native checks; create the advisory `Integrate` label. Shadow mode
   reports checks/dispatches CI but never authorizes, stages, or publishes merges.
7. Complete the acceptance rehearsal below. Drain/reconcile Gerrit CE work,
   disable the old public import/export writer, activate the reviewed settings,
   set `mode: active`, and set repository variable `CE_MERGE_MODE=active` to retire
   the scheduled advisory labeler. Do not run both publication paths as writers.

```sh
python3 -W error::ResourceWarning -m unittest discover -s scripts/ce_merge/tests -v
python3 -m scripts.ce_merge --policy /etc/mysql-ce-merge/policy.json rulesets
python3 -m scripts.ce_merge --policy /etc/mysql-ce-merge/policy.json verify-deployment
python3 -m scripts.ce_merge --policy /etc/mysql-ce-merge/policy.json --state-dir /var/lib/mysql-ce-merge/journal serve
```

Install `scripts/ce_merge/deploy/mysql-ce-merge.service` only after provisioning
its service user, checkout, credentials, and state directory. Webhooks listen on
loopback `/webhook`; terminate TLS and enforce request/concurrency limits at the
ingress. HMAC, App/installation identity, current maintainer permission, and action
identity checks protect authorization. Duplicate deliveries reuse the operation.

### CI contract

`CE Merge Validation` runs the exact staged commit on disposable hosted runners,
using trusted scripts from the automatically resolved workflow revision. Candidate checkout has
read-only credentials that are not persisted, no repository secrets, and no shared
cache. Every job verifies the candidate SHA and its complete parent list before
running code. The coordinator accepts only its App's workflow dispatch at the
recorded SHA, with all seven required jobs and their required steps successful:
GCC/Clang builds, four MTR shards, unit tests, and formatting.

Only `ci_ref` is configured. The recorded revision also appears in the run name
and the `workflow_revision` dispatch input. Every job verifies that the workflow
actually runs at that revision before executing candidate code. If the ref moves,
the next poll records its new SHA and dispatches a fresh request, without waiting
for the old revision's dispatch throttle. A run caught by a ref movement cannot
satisfy the new request. Normal PR/base/candidate changes also require fresh CI.
Request records survive restarts and lost dispatch responses. `rerun-ci` accepts
only recorded App runs at the current resolved ref; after a ref change, let normal
polling dispatch the replacement run. Deploy this workflow update together with
the coordinator because its dispatch inputs and run-name format have changed.

Only `Docs/**`, `README`, `README.md`, and `CONTRIBUTING.md` qualify for an explicit
documentation-only build exemption. Missing, skipped, foreign, or stale runs do
not pass. Null merges still require review and CI. Trusted CI has no automatic
quarantine; approve/rehearse any branch-specific test policy before cutover.

## Conflict resolution and recovery

`status` and `CE / operation` show the batch state and candidate/PR mapping.
Monitor blocked/conflict/uncertain states, CI age, pending reviews, and private
sync age. A long review wait leaves CE refs unchanged.

```mermaid
stateDiagram-v2
    [*] --> queued: maintainer authorizes
    queued --> prepared: snapshot and prepare
    prepared --> conflict: upmerge needs resolution
    conflict --> prepared: reviewed resolution PR
    prepared --> blocked: changed authorization or policy
    prepared --> publishing: all gates pass; save exact intent
    publishing --> complete: atomic receipt confirmed
    publishing --> uncertain: lost response or rejected push
    uncertain --> complete: reconcile receipt
    uncertain --> publishing: explicit retry of same transaction
    blocked --> aborted: cancel unpublished batch
```

For a conflict, the bot stages the lower candidate in its fork and opens a PR
against the conflicting target. The check lists the exact lower candidate and
base SHAs. In your own fork, fetch that bot branch, start a resolution branch from
the recorded target base, merge the lower candidate, resolve conflicts, and commit.
Open a resolution PR against that target. Do not squash away either parent.
After OCA and current-head approval from a reviewer with Write, Maintain, or Admin access, the merge captain runs:

```sh
python3 -m scripts.ce_merge repair-upmerge OPERATION_ID --pr RESOLUTION_PR
```

The App requires both exact inputs in the resolution ancestry, rescans content,
freezes its head/body, rebuilds the remaining chain, and runs CI on the resolved
candidate. Another conflict repeats this process. Earlier unchanged candidates
retain their reviews; changed higher candidates need new PRs/reviews. Resolution
cannot turn an approved null merge into a content change. The original PR's own
conflict must be fixed in that PR and newly authorized. Security promotion conflicts
require freshly approved release manifests, not the ordinary repair command.

Use deployed `--policy` and `--state-dir` arguments before each subcommand:

```sh
python3 -m scripts.ce_merge status
python3 -m scripts.ce_merge audit
python3 -m scripts.ce_merge rerun-ci RUN_ID
python3 -m scripts.ce_merge resume OPERATION_ID
python3 -m scripts.ce_merge abort OPERATION_ID
python3 -m scripts.ce_merge reconcile OPERATION_ID
python3 -m scripts.ce_merge retry OPERATION_ID
python3 -m scripts.ce_merge resolve-rejected OPERATION_ID --fencing-record RECORD
```

`resume` rechecks unchanged unpublished work. If a source revision/body, target
snapshot, or policy changes, abort the unpublished batch and obtain fresh Integrate
authorization; old candidate PRs are superseded and must not be merged manually.
Abort creates no CE changes and clears cached eligibility so a fresh action can
be offered. Completed and uncertain operations cannot be aborted.

An uncertain response is never interpreted as failure solely because of time.
First reconcile the immutable receipt. If present with its expected object ID,
publication succeeded. Otherwise `retry` revalidates and sends the **same entire
transaction**, using the same receipt and old-SHA leases. Before abandoning an
unconfirmed attempt, stop/fence the old process and credentials, confirm no receipt
and all original target heads, then use `resolve-rejected` with evidence. Never
force-push, delete published history, or delete journal files to unlock a batch.
PR reporting errors retry independently after receipt confirmation.

## Daily private integration

Capture a completed CE snapshot with `python3 -m scripts.ce_merge snapshot` using
the deployed policy and state-directory arguments. The command takes the worker exclusion
lock and refuses an uncertain publication or release hold. Prepared work has not changed any CE branch. Schedule this from
the existing private scheduler once daily, with merge-captain-approved urgent runs.

On a separate private host, create an operation specification. Source and target
repositories are absolute paths to trusted local clones. `source_sha` values come
from the recorded CE snapshot, and `before` values are approved downstream heads.
Branches must be ordered oldest to newest. For example:

```json
{
  "kind": "ce-sync",
  "source": "/private/clones/ce",
  "targets": [{
    "repository": "/private/clones/sec",
    "push_url": "PRIVATE_SEC_REMOTE",
    "branches": [{"name": "trunk", "before": "TARGET_SHA", "source_sha": "CE_SHA"}],
    "validate": [["/private/bin/validate-sec"]]
  }]
}
```

Include EE as a second target for independent CE-to-EE merging. The runner stages
explicit non-fast-forward merges, upmerges each lower result into the next branch,
and runs the configured validation commands. Conflicts retain the workspace and
fail; no automatic ours/theirs strategy is used. Remote publication uses normal
fast-forward, atomic multi-ref pushes per target repository. Cross-repository
publication is not atomic; the retained intent and resulting SHAs allow retries
to skip a target that already received its results.

```sh
python3 -m scripts.ce_merge.private /private/sync.json --state-dir /private/sync-journal
python3 -m scripts.ce_merge.private /private/sync.json --state-dir /private/sync-journal --apply
```

The first command stages and validates; it does not push. Repeating the exact
specification reuses its durable prepared result. A failed preparation leaves a
workspace for investigation: archive it before a new attempt. A moved remote head
requires fresh snapshots and a new specification. Private validation logs and
ledgers must not be uploaded to public Actions or public issue comments.

For SEC-to-EE delivery, use `kind: security-pick` with SEC as `source`, EE as the
target, and `picks: [{"kind":"SC","sha":"...","item":"..."},
{"kind":"ST","sha":"...","item":"..."}]` on each target. SC changes must
exclude private paths; ST changes must be confined to `internal-sec/`. Each pick
produces its own `-x` commit and source/result ledger entry. The release ledger
selects only undelivered items; empty or conflicting picks stop for reconciliation.

## Release-time security promotion

Before release, use `kind: prepare-promotion`, EE as the source, and a clean CE
baseline clone as the target. Select only SC picks; provide independent
`security_approval` and `release_approval` records. Each target branch also needs
`expected_public_tree`, the tree SHA of the privately validated, filtered CE
release source. Configured validation commands must build/test that CE source and
verify its release equivalence. Development and release targets are separate when
their baselines differ.

Preparation cannot push. It creates an approved-SC bundle with the CE baseline as
a prerequisite and a private ledger. The working clone contains private objects:
**never copy or push that clone to a public fork**. Transfer only the approved
bundle and publication manifest through the authorized release process.

```mermaid
sequenceDiagram
    participant R as Release team
    participant P as Private preparation
    participant C as CE coordinator
    participant G as Public GitHub
    R->>P: Approved EE SC list and frozen CE baseline
    P->>P: Pick SC, validate tree, build and test
    P-->>R: Sanitized CE-parented bundle and ledger
    R->>R: Publish corresponding product release
    R->>C: Release hold and approved publication manifest
    C->>G: Verify published release evidence
    C->>G: Push only approved CE-parented head to bot fork
    C->>G: Create promotion PR
    G-->>C: Independent review and exact-candidate CI
    C->>G: Publish all approved target refs atomically, including ancestry
    R->>R: Final validation, public source tag, private synchronization
    R->>C: Lift hold with validation and synchronization records
```

The implemented release-evidence adapter requires a **published GitHub Release**
in the configured CE repository with a matching `release_id` and `release_tag`.
For product releases announced elsewhere, the release team must publish the
corresponding GitHub release evidence after actual product release, or implement
and rehearse an equivalent authenticated adapter before enabling promotion.
Do not use an approval timestamp or a draft release as publication evidence.

Declare every target on the hold with repeated `--target` arguments. Development
targets must include every successor through Innovation. Register one manifest
per target. Higher development targets need an `ancestry_reason` explaining why
their independently prepared SC tree supersedes lower content. The generated PR
retains that tree and the lower candidate as an additional parent; review is
mandatory. No target publishes until all manifests, reviews, and CI are complete.
The prepared steps freeze the batch's target set. The coordinator revalidates that
set against the release hold before advancing, immediately before publication,
and on retries. Changing hold targets invalidates the prepared batch: abort the
unpublished operation, register the manifests for the revised target set, and
authorize a new batch. A hold reason change does not invalidate the batch.

If validation or a conflict requires rebuilding a promotion, abort any unpublished
operation for that release first, then run `publish-promotion` with the replacement
manifest (or `register-promotion` for an existing open replacement PR). Registration
selects one active manifest per release and target. Previous PR manifests and audit
events remain recorded, but superseded PRs cannot authorize or enter a new batch.
Closing a PR alone does not change the selected manifest. Reauthorize the batch
from its active oldest-target PR after replacement. Uncertain publication must be
reconciled first, and a completed release cannot be replaced.

When removing a target, abort all unpublished operations for the release, change
the hold to the revised target list, and explicitly retire each removed target:

```sh
python3 -m scripts.ce_merge retire-promotion --release-tag PRODUCT_RELEASE_TAG --target REMOVED_BRANCH --reason 'Release owner approved reduced scope'
```

Retirement preserves the PR manifests and audit history. A persistent retirement
record prevents old manifests from reappearing after restart; the retired PR cannot
enter a batch. Retirement is rejected while release operations are non-aborted,
publication is uncertain, or the target remains in the hold. Register any required
replacement manifests, then authorize a new batch from the oldest active target.

For journals created before active-manifest tracking, an unambiguous manifest per
target remains usable. If a target already has several historical entries, register
the approved replacement explicitly; the coordinator will not guess which one wins.

Publication manifest fields:

```json
{
  "release_id": 123, "release_tag": "PRODUCT_RELEASE_TAG",
  "base": "trunk", "base_sha": "CE_BASE_SHA", "head": "PREPARED_CE_HEAD",
  "commits": ["FIRST_CE_PICK_SHA", "LAST_CE_PICK_SHA"],
  "expected_tree": "VALIDATED_PUBLIC_TREE_SHA",
  "bundle": "/private/approved-sc.bundle", "bundle_ref": "refs/heads/ce-promotion-0",
  "security_approval": "SECURITY_SIGNOFF_RECORD",
  "release_approval": "RELEASE_SIGNOFF_RECORD",
  "validation_record": "PRIVATE_VALIDATION_RECORD"
}
```

Only trusted release operators with service-account access may import manifests;
the signoff fields reference existing private approval records, not self-service
claims from contributors. Keep manifests and the coordinator journal private.

```sh
python3 -m scripts.ce_merge hold --reason 'Released security promotion' --release-tag PRODUCT_RELEASE_TAG --target trunk
python3 -m scripts.ce_merge publish-promotion /private/publication.json
# Repeat publication for EVERY declared target; review each source PR.
# Authorize Integrate on the oldest target, then review all generated candidates.
python3 -m scripts.ce_merge unhold --validation-record RECORD --sync-record RECORD
```

Use deployed policy and state-directory arguments on these commands. Publication
checks actual release evidence before the first public write, verifies the complete bundle
ancestry and tree, and pushes only the approved head. It registers the resulting
PR against its exact head. `register-promotion` supports an already-created,
authorized PR using the same manifest plus `pr`, with the same release gate.
Both paths require `expected_tree` and verify that the approved head's tree matches
it, along with the approved commit list and ancestry. The coordinator repeats this
validation when evaluating, preparing, publishing, or retrying a promotion; CI cannot substitute for
validated release-source equivalence. Legacy manifests missing `expected_tree`
must be replaced with an approved manifest before proceeding.

After promotion, the release pipeline validates the final CE source and uses its
separate release App to create the annotated public source tag at the recorded
release revision, never at an arbitrary development tip. The tag must be distinct
from any preexisting announcement tag if its source revision differs; never move
an existing tag. Run CE-to-SEC and CE-to-EE reconciliation privately before lifting
the hold. `unhold` requires operator evidence references, the complete declared
promotion batch published, and complete development-branch ancestry. Existing release validation,
tag publication, private scheduling, and organizational signoff systems remain
external integration points; their success must not be inferred from a PR merge.


## Acceptance before production activation

The ordinary PR CI reporter intentionally withholds trusted status when a PR edits
the workflow that produced a result. Builds and tests can succeed while their
reported status says `PR edits this workflow; result is not trusted`. The reporter
change itself takes effect only after it reaches the trusted default branch.
Review bootstrap workflow changes and their run logs explicitly; rerunning an
unchanged PR does not remove this restriction. CE validation remains a separate
workflow at the configured trusted ref and still requires deployment and configuration.

Local tests exercise real disposable Git repositories plus a simulated GitHub API.
They do not establish that production GitHub rulesets, reviews, credentials, or
MySQL builds are configured correctly. Demonstrate these in non-production GitHub:

- Original PRs from both forks and same-repository branches, plus all generated
  upmerges, are reviewed; all CE refs publish
  together and preserve each lower branch's ancestry in its successors.
- Missing OCA, stale approval/head, unauthorized action, unresolved threads,
  failed/skipped CI, forbidden commit history, and policy drift block publication.
- Humans cannot push/merge into configured CE targets; App pushes missing either trusted check fail; valid
  atomic App publication succeeds; force pushes and tag changes are rejected.
- Reject one target or the receipt and observe no requested ref changes. Competing
  publishers cannot replace snapshots. An unsupported atomic server fails closed.
- Conflict resolution, reviewed null merges, crashes, duplicate events, and lost
  responses recover without partial publication or duplicate integration.
- GitHub accepts ancestry-only PRs and updates PR UI as expected; squash closure
  and receipt links clearly identify the integrated result.
- Run trusted CI on every actual LTS branch; no missing suites or quarantine gaps.
- CE ancestry is reachable in both SEC and EE; SC/ST stay separate; only approved
  SC becomes public after the real release, with validated release-source tags.
- Verify CE bisect and EE first-parent bisect on the rehearsal history.

Daily scheduling, organizational 2FA, release signoff systems, final source tag
publication, and live repository administration remain deployment responsibilities.
To pause rollout, disable new integration requests, stop the executor safely, and
reconcile any publication intent. Preserve all already-published history.
