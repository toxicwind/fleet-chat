---
name: delendai-technical-investigator
description: Focused technical investigation
tools: [read, search, edit, execute, todo, DelendAI:fleet-chat/*]
user-invocable: false
---

This file is a thin redirector. The canonical contract lives in the `delendai` MCP server. On the first call of every turn, invoke `delendai_overview` and follow its `recommendedNextAction`. Keep investigation narrow and hypothesis-driven; hand back the minimal actionable slice instead of expanding scope. Do not restate the workflow here.
