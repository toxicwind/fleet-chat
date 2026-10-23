---
name: delendai-error-collection
appliesTo: ['@delendai/*']
description: How to use the core error-collection engine — when to call ctx.errorCollector.record directly vs withErrorCollection, how to write a custom IErrorSink, the privacy guarantees, and the autoReport opt-in for the issues plugin. Use when adding error capture to a plugin handler or writing a new sink.
---

# Error-collection engine (f00251)

The core ships a classification → fingerprint → redaction → fan-out pipeline.
Any plugin handler can capture errors into the pipeline; all registered sinks
receive the same pre-redacted `ICapturedError` event in parallel.

## Summary

`createErrorCollector({ sinks })` builds the engine. The assembler wires it
automatically when a plugin returns `errorSinks` from `register()`. The engine:

1. **Classifies** the raw error into a severity band and a human-readable tag.
2. **Fingerprints** the event (SHA-256 of `packageId|toolName|errorCode|stackHead`).
3. **Redacts** all text fields once via `createDefaultRedactionPolicy`.
4. **Fans out** to every registered sink in parallel (`Promise.allSettled`).

## When to use `ctx.errorCollector.record` vs `withErrorCollection`

| Situation | Use |
|-----------|-----|
| Tool handler that may throw (HTTP, FS, LLM errors) | `withErrorCollection(handler, { toolMeta, collector })` |
| You need the redacted event for a secondary hook | `withErrorCollection` with `onError` callback |
| Recording a pre-caught error at a specific point | `await ctx.errorCollector.record(caught, context)` |
| Testing a single sink without booting the server | `createErrorCollector({ sinks: [mySink] })` directly |

`withErrorCollection` is the preferred path for handlers — it wraps cleanly,
re-throws the original error unchanged, and never swallows.

## How to write a custom `IErrorSink`

```ts
import type { IErrorSink, ICapturedError } from '@delendai/core/public';

class SentrySink implements IErrorSink {
  readonly id = 'sentry';

  async record(event: ICapturedError): Promise<void> {
    // MUST NOT throw — handle failures internally.
    try {
      await Sentry.captureEvent({
        message: event.summary,   // already redacted by the core
        level: event.severity,
        fingerprint: [event.fingerprint],
        extra: { toolName: event.toolName, pluginName: event.pluginName },
      });
    } catch (err) {
      // Swallow or write to a secondary fallback — never rethrow.
      process.stderr.write(`[sentry-sink] record failed: ${String(err)}\n`);
    }
  }
}
```

Return it from `register()`:

```ts
export default definePlugin({
  name: 'my-plugin',
  register(ctx) {
    const sink = new SentrySink();
    return {
      tools: [...],
      errorSinks: [sink],   // ← collector picks this up
    };
  },
});
```

**`record` MUST NOT throw.** A throwing sink aborts fan-out for itself but the
collector guards each call via `guardedRecord` — a sink failure is forwarded to
the `onSinkError` callback and does not propagate to the caller or other sinks.

## Privacy guarantee

Secrets are redacted **before** any sink sees the event:

1. `createDefaultRedactionPolicy` runs once in the collector engine and produces
   a single redacted copy (`summary`, `stackHead`, path collapse).
2. `ConsoleErrorSink` and `createLogsErrorSinkAdapter` each apply `redactSecrets`
   a second time as defense-in-depth at their own boundary.

Even the fallback `ConsoleErrorSink` (active only when zero real sinks are
registered) re-redacts before writing to `process.stderr`.

## `ConsoleErrorSink` fallback suppression

When at least one plugin returns an `IErrorSink` via `errorSinks`, the assembler
uses those sinks and does **not** add `ConsoleErrorSink`. The console fallback is
only active when no plugin registers a sink.

## autoReport opt-in (issues plugin)

The `issues` plugin adapter writes redacted markdown drafts by default
(safe-mode). To open live GitHub issues:

```jsonc
{
  "plugins": {
    "issues": {
      "options": {
        "repo": "owner/name",
        "autoReport": true,       // explicit opt-in
        "maxReportsPerHour": 5    // default
      }
    }
  }
}
```

Live issue creation is gated on: `autoReport === true` AND severity ≥ `critical`
AND the fingerprint is not already in the rolling-hour dedup window AND the
hourly rate limit has not been reached. **Safe-mode is the default**; live writes
require an explicit operator decision because they touch the network.

## References

- Proposal: `f00251` (S1 core engine, S2 public surface + wiring, S3 logs adapter, S4 issues adapter, S5 skill + smoke test).
- Precedent: `f00154 S2` (`ILogsSink` — the parallel port for lifecycle events).
