# Open development model (proposed)

> Status: **proposal / horizon 3** — engineering working model, not a governance
> or licensing change. The license (GPLv2) and the OCA are unchanged.

This document describes the target development workflow the earlier horizons
build toward. The single organizing idea is **directionality**.

## The directionality principle

Today MySQL's public GitHub tree is effectively a **downstream mirror**: canonical
development happens internally and lands on the public tree on a publish cadence.
External pull requests are re-keyed into an internal tracker before they can be
acted on. The public tree being downstream is the root cause of most contributor
friction — invisible CI, delayed review, merge windows that don't line up with
where the internal tree already moved.

The proposal is to **invert the arrow**:

```
   CURRENT (public tree downstream)        TARGET (public tree upstream)

   internal trunk  ──► publish ──►         public trunk (canonical)
        ▲                                       │
   external PR ─► re-key ─► internal            ├─► security embargo branch  (downstream)
                                                ├─► commercial LTS trains     (downstream)
                                                └─► closed perf/CI infra      (downstream)
```

The public tree becomes the **upstream of record**. Internal work does not
disappear — it becomes a set of **bounded, well-defined downstream areas** that
consume the public tree rather than feed it.

## This is the OpenJDK model

OpenJDK demonstrates that a commercially sponsored project can run public-upstream
while still accommodating legitimate internal-only workflows. The public tree is
canonical; vendor-internal work (security embargoes, commercial LTS trains, closed
test/perf infrastructure) sits downstream in bounded areas. MySQL inverts this
relationship today; this proposal aligns it with the OpenJDK pattern without any
change to license or contributor agreement.

## Legitimate downstream areas (stay internal, by design)

Directionality does not mean "everything is public." These remain internal, and
that is correct:

1. **Security embargoes.** CVE work happens on a private branch until coordinated
   disclosure, then merges to public trunk. (Same as OpenJDK's vulnerability group.)
2. **Commercial / LTS release trains.** Enterprise-only backports and packaging
   are downstream consumers of public trunk, not a parallel source of truth.
3. **Closed performance and test infrastructure.** Large internal benchmarking and
   hardware-specific CI can gate releases without being the canonical tree.

The boundary is explicit: anything *not* in those three categories develops in
the open by default.

## External committer write model

With the public tree canonical, trusted external contributors can earn commit
access to defined areas (mirroring OpenJDK Committer/Reviewer roles), bounded by
`CODEOWNERS`:

- **Contributor** — opens PRs (today's ceiling for external participants).
- **Area committer** — merge rights within specific paths after a track record;
  changes still require a maintainer review and green CI.
- **Maintainer/Reviewer** — owns an area, reviews, holds the triage SLAs in
  `CONTRIBUTING.md`.

This is the row your benchmark matrix calls "external committer write model" —
the capability MySQL currently lacks and PostgreSQL, MariaDB, OpenJDK, and
Valkey all have.

## How the horizons ladder up to this

| Horizon | Change | Why it's a prerequisite for directionality |
|---|---|---|
| 1 | GitHub-native CONTRIBUTING, templates, in-repo build guide | Makes the public tree usable as the entry point |
| 2 | PR CI (build + smoke MTR), devcontainer, OCA automation, auto-routing | Gives the public tree a real, fast feedback loop — the thing a canonical tree must have |
| 3 | RFC process, external committer model, bounded downstream areas | Completes the inversion: public trunk becomes upstream of record |

You cannot flip directionality without horizons 1–2 first: a canonical tree that
can't give contributors automated feedback isn't actually canonical.
