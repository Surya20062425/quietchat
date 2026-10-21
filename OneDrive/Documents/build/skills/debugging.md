---
name: debugging
description: Systematically diagnose a failure or bug instead of guessing at fixes
version: 1
tags: [debug, testing, troubleshooting]
platforms: [cli]
---

Never guess-and-patch. Reproduce, then bisect the cause.

1. **Reproduce** — run the exact failing command yourself. Capture the full output and
   exit code, not a paraphrase. If you cannot reproduce it, say so and stop.
2. **Read the real error** — the last 20 lines, plus the first `Traceback` frame that is
   inside project code (not stdlib). Never edit code you have not read.
3. **Locate** — narrow with `search_files` for the error string, the symbol in the trace,
   or the failing input. Form one falsifiable hypothesis.
4. **Test the hypothesis** — cheapest possible check: a print, a focused test, a single
   reproduction. If the hypothesis is wrong, discard it and go back to step 3.
5. **Fix the cause**, not the symptom. If you must work around something, say so plainly
   and explain what you are deferring.
6. **Verify** — re-run the original reproduction. A fix that is not verified is a guess.

Summarize as: symptom → root cause → fix → how you verified. State clearly when the root
cause is unknown; a wrong confident answer costs more than an honest uncertainty.