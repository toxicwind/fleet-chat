---
name: delendai-implementation-runner
description: Slice executor (atomic writes with locks)
---

This file is a thin redirector. The canonical contract lives in the `delendai` MCP server. On the first call of every turn, invoke `delendai_overview` and follow its `recommendedNextAction`. Claim files before writing with the agent-lock tool `overview` reports; a hardcoded tool list here would go stale. Do not restate the workflow here.
