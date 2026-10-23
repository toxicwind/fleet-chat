---
name: delendai-orchestrator
description: Multi-agent orchestrator for delendai
---

This file is a thin redirector. The canonical contract lives in the `delendai` MCP server. On the first call of every turn, invoke `delendai_overview` and follow its `recommendedNextAction`. For non-trivial work, delegate through the swarm-coordination tools `overview` reports. Do not restate the workflow here — hardcoded tool names rot within days.
