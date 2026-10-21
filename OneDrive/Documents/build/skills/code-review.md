---
name: code-review
description: Review a diff or file for correctness, readability, and risk before merging
version: 1
tags: [review, quality, git]
platforms: [cli]
---

When reviewing code, work through these in order and stop reporting once the signal
drops — lead with real defects, not style preferences.

1. **Correctness** — does it do what the author claims? Trace the actual control flow,
   including error paths and off-by-one boundaries.
2. **Interfaces** — are public signatures and return types changed compatibly? Check every
   caller with `search_files` before declaring a change safe.
3. **Failure modes** — unhandled exceptions, swallowed errors, partial writes. Prefer
   explicit failure over silent degradation.
4. **Security** — unvalidated input crossing a trust boundary, secrets in logs, command
   construction from untrusted strings.
5. **Tests** — is the risky path covered? Name the specific missing case, do not say
   "add more tests".

Report each finding as: file:line — what breaks — smallest fix. If nothing is wrong,
say so plainly and name the one thing you checked most carefully. Do not invent findings
to look thorough.