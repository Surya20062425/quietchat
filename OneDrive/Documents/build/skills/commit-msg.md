---
name: commit-msg
description: Write a commit message that explains why the change exists
version: 1
tags: [git, writing]
platforms: [cli]
---

Inspect before writing: `git diff --staged` and `git log -5 --oneline` to match the
repository's existing message style.

Format:

```
<type>(<scope>): <imperative summary under 72 chars>

<why the change was needed — the problem, not the mechanics>

<notable trade-offs, migrations, or follow-ups>
```

Types: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `perf`, `build`.

Rules:
- The summary line states intent ("fix null deref in retry path"), never restates the diff
  ("update retry.go").
- Body explains motivation and reasoning. Do not re-describe the diff — the diff is right
  there in the terminal.
- One logical change per commit. If the message needs "and", split the commit.
- Omit the body entirely for trivial changes; do not pad it.