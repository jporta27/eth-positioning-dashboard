---
description: Review the current uncommitted diff against the eng-practices checklist in CLAUDE.md
---

You are reviewing the current CL (uncommitted changes + staged changes) in this repo. Apply the checklist defined in the "Code review standard" section of `CLAUDE.md`.

## Step 1 — Gather the diff

Run these in parallel:
- `git status` to see touched files
- `git diff --stat` to see size
- `git diff` to see the actual changes (staged + unstaged together)

If there is nothing to review, say so and stop.

## Step 2 — Apply the checklist

For each category in the checklist (Blocking, Important, Nits), report findings as a markdown list. Use these labels inline:

- 🔴 **Blocking** — must fix before merge (items 1–5 in the CLAUDE.md checklist)
- 🟡 **Important** — fix in this CL or as immediate follow-up (items 6–8)
- **Nit:** — non-blocking style suggestion

Be specific: cite file paths and line numbers from the diff. Quote the offending code when useful. Do NOT just say "magic number found" — say *which* number, *where*, and *what name* you'd give it.

## Step 3 — Special checks for this repo

- **Two-backend sync.** If the diff touches `backend/main.py` OR `api/index.py` but not both, grep the other file for the modified function names and report whether the change should be mirrored. Quote the relevant section of the unmodified file.
- **Smoke tests.** If the diff touches either backend, remind the user to run `python scripts/smoke_tests.py` before pushing.
- **CL size.** If `git diff --stat` shows >200 net lines added, suggest concrete splits based on the natural boundaries listed in CLAUDE.md (fetch / processing / frontend).
- **Frontend re-render hazards.** If the diff touches `frontend/src/Dashboard.jsx`, scan for inline object/array literals inside component bodies and IIFEs computing derived state — flag candidates for `useMemo` or extraction.

## Step 4 — Summary

End with a clear **"Verdict"** block:

```
VERDICT: <APPROVE | APPROVE WITH FIXES | BLOCK>

Blocking items to fix before merge:
  - <numbered list, or "none">

Suggested follow-up CLs:
  - <list, or "none">
```

If verdict is APPROVE, suggest a commit message that follows the descriptions pattern (subject ≤70 chars + optional body explaining the WHY).

## Tone

Direct, concise, no hedging. Cite the CLAUDE.md rule number when flagging a blocker (e.g., "rule 3 — magic numbers"). Skip diplomatic preambles.
