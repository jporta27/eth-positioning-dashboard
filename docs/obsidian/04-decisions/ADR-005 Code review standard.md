---
tags: [adr, process]
status: accepted
date: 2026-05
---

# ADR-005 — Code review standard (Google eng-practices)

**Status**: Accepted
**Context**: After accumulating ~10k lines of code across two backends + a 4500-line frontend, we needed a consistent rubric to keep new changes from compounding the drift and magic-number problems we already had.

## The decision

Adopt a checklist based on [Google's eng-practices](https://google.github.io/eng-practices/) adapted to this repo. Documented in `CLAUDE.md` and invokable via `/review-cl` slash command.

## The checklist

### Blocking (must fix before merge)

1. **Two-backend sync.** Any change to data logic must touch both `backend/main.py` AND `api/index.py`. Grep both files after editing.
2. **Smoke tests pass.** `python scripts/smoke_tests.py` must exit 0 when either backend is touched.
3. **No magic numbers.** Inline numeric thresholds → named constants at module top, with WHY comments.
4. **Comments explain WHY.** Restating what the code does is noise. The thresholds, the engine version requirements, the fallback chains — these all need WHY.
5. **Mirror file comments.** A function in both backends should have **identical** explanatory comments. A reader landing in either file should get the same WHY.

### Important (this CL or immediate follow-up)

6. **CL size.** <200 net lines per CL. Natural splits: (a) fetcher, (b) processing + constants + test, (c) frontend rendering.
7. **API response field naming.** camelCase at the boundary. snake_case internal.
8. **New derived metrics need coverage.** Smoke test assertion OR unit test in `backend/tests/`.

### Nits (non-blocking)

- Style consistency, naming, micro-optimizations.

## Invocation

```
/review-cl
```

Reads the current `git diff` (staged + unstaged) and produces a markdown report mapping findings to the checklist rules.

## Retroactive application

Applied to commit `ea7f365` (the Etherscan mainnet integration) which introduced 3 blockers:
- Rule 3 (magic numbers 0.8 / 0.3)
- Rule 5 (comments not mirrored)
- Rule 8 (no coverage for hedge_label)

Resolved in 3 follow-up CLs:
- `CL-A` (4b7c3d6): constants extracted
- `CL-B` (9945cc1): comments mirrored
- `CL-C` (f87fabf): unit tests + smoke check added

## Why not stricter

Considered options like mandatory pre-commit hooks, CI-blocked merges, lint enforcement. Skipped because:
- The repo is single-author (so far)
- Hooks would slow down rapid iteration
- The checklist as **documentation** + manual review has been sufficient

If a contributor joins, escalate to enforcement.

## See also

- `CLAUDE.md` (canonical source of the checklist)
- `.claude/commands/review-cl.md` (slash command definition)
- [[Smoke tests]]
