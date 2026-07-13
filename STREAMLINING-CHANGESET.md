# MySQL contribution streamlining — fork changeset

A concrete, appl-able set of changes to a `mysql-server` fork demonstrating how
the contribution process can be streamlined. Every file mirrors where it would
live in the real tree, so this can be applied directly. **No license or OCA
change** — this is engineering workflow only.

## The "before" state (verified against `mysql/mysql-server` trunk)

- `CONTRIBUTING.md` is OCA-and-bug-tracker centric; GitHub PRs are a secondary
  path routed through `bugs.mysql.com`.
- No build quickstart in-repo, no PR/issue templates, no per-PR CI, no one-command
  dev environment, no published triage SLAs, no RFC process.
- The public tree functions as a **downstream mirror** of internal development.

## What this changeset adds, by horizon

### Horizon 1 — make contributing legible (low risk, days)
| File | Change | Modeled on |
|---|---|---|
| `CONTRIBUTING.md` | Rewritten GitHub-native flow; build → test → PR in one read; OCA preserved; published triage SLAs | PostgreSQL, ClickHouse |
| `.github/PULL_REQUEST_TEMPLATE.md` | Prompts reviewers always need | Hugging Face Transformers |
| `.github/ISSUE_TEMPLATE/*` | Structured bug/feature forms + security redirect | ClickHouse |
| `docs/development/BUILD-FROM-SOURCE.md` | Fast-path build guide kept in sync with CI | PostgreSQL |

### Horizon 2 — a real feedback loop (weeks)
| File | Change | Modeled on |
|---|---|---|
| `.github/workflows/pr-build.yml` | Debug build on gcc+clang, Boost+ccache caching | MariaDB, Valkey |
| `.github/workflows/mtr.yml` | Default MTR test selection on every PR | Valkey/CNCF |
| `.github/workflows/clang-format.yml` | Format check using the **existing** `.clang-format` | ClickHouse |
| `.github/workflows/labeler.yml` + `.github/labeler.yml` | Auto-route PRs to area owners | Kubernetes/Valkey |
| `.github/workflows/stale.yml` | Gentle hygiene, only for `needs-info` | OpenJDK triage norms |
| `scripts/ci/*` | `bootstrap`/`build`/`mtr`/`format` so **local == CI** | PostgreSQL |
| `.github/CODEOWNERS` | Review routing + basis for committer model | OpenJDK |

### Horizon 3 — open development model (quarters)
| File | Change | Modeled on |
|---|---|---|
| `docs/development/open-development-model.md` | The **directionality** flip: public tree as upstream; bounded internal downstream areas; external committer write model | OpenJDK |
| `docs/rfcs/*` | Lightweight RFC process so direction is checked before code is written | OpenJDK JEPs (lightweight) |

## How to apply to a real fork

```bash
git clone https://github.com/<you>/mysql-server && cd mysql-server
git checkout -b devex/streamline
rsync -a --exclude STREAMLINING-CHANGESET.md /path/to/this/changeset/ .
git add -A && git commit -m "devex: streamline contribution process (H1–H3 PoC)"
```

The `.clang-format` already in the tree is reused as-is; nothing here redefines
code style. OCA verification is handled separately by Oracle rather than by a
workflow in this repository.

## The one-line argument

Horizons 1–2 give the public tree a usable entry point and a real feedback loop;
horizon 3 then flips **directionality** so the public tree becomes the upstream of
record — exactly the OpenJDK pattern, with security embargoes, LTS trains, and
closed perf infra remaining as bounded downstream areas.
