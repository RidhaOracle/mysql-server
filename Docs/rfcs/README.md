# MySQL RFCs

Substantial changes — new SQL syntax, on-disk format changes, protocol changes,
or anything altering public behavior — start with an RFC. This exists so external
contributors learn early whether a direction aligns with internal plans, instead
of discovering a conflict after writing the code. That mismatch is the single
most common reason external work has historically stalled.

## Process

1. Copy `0000-template.md` to `NNNN-short-title.md` (next free number).
2. Open a PR adding it; discussion happens on the PR.
3. A maintainer records the decision within the RFC SLA in `CONTRIBUTING.md`
   (target: 15 business days).
4. Accepted RFCs merge with status `Accepted` and a tracking issue; implementation
   PRs reference the RFC.

Small, behavior-preserving fixes do **not** need an RFC — just open a PR.
