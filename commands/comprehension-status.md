---
description: Show the comprehension-gate evidence for the current branch (authorship, versions, time, risk)
argument-hint: "[base-branch]"
---

Run this command and summarise the result for the user in a short table. Do not
answer any comprehension questionnaire on the user's behalf - those answers must
be the code owner's own.

```
python "${CLAUDE_PLUGIN_ROOT}/comprehension_gate.py" evidence --base ${1:-origin/main} --head HEAD
```
