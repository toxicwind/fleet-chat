---
name: delendai-delivery-verifier
description: Acceptance and gates verifier
---

This file is a thin redirector. The canonical contract lives in the `delendai` MCP server. On the first call of every turn, invoke `delendai_overview` and follow its `recommendedNextAction` — it lists the live quality-gate and proposal-review tools, which change often enough that a hardcoded list here would go stale. Do not restate the workflow here.
