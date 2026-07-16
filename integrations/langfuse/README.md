# Vibe Code Game Studio Langfuse Adapter

The compatibility directory name remains `ccgs`, but new exports use the
versioned neutral Observability Port. The adapter never owns workflow state,
Evidence, Closeout, retry policy, or consumer-project rules.

Because the Codex client does not expose its internal model call telemetry, this
adapter does not fabricate prompts, token counts, model costs, or generations.
It observes only stable workflow identities, status, project-relative
references, a context digest, failure codes, and explicitly declared metrics.

## Current Transport

Trace and span data use Langfuse's recommended OpenTelemetry endpoint:

```text
/api/public/otel/v1/traces
```

The adapter sends `x-langfuse-ingestion-version=4` and Basic authentication from
environment variables. Explicit Score objects use the current public
`POST /api/public/scores` compatibility endpoint. Langfuse currently labels the
Score POST as legacy, so trace export remains independent and Score failure is
reported separately.

Official references:

- [Native OpenTelemetry integration](https://langfuse.com/integrations/native/opentelemetry)
- [Langfuse API reference](https://api.reference.langfuse.com/)
- [Observability data model](https://langfuse.com/docs/observability/data-model)

## Event Contract

Events must be JSON files below:

```text
{data_dir}/production/observability/events/
```

They conform to `schemas/langfuse-workflow-event.schema.json`. This is the
compatible local event contract and may contain bounded human-readable
summaries used by local tooling. It is never sent directly to Langfuse.

Before export, `.ccgs-core/scripts/vibe_observability.py` projects the local
event into `schemas/observability-request-data.schema.json`. The remote event
allowlist contains:

- stable `event_id` and `trace_key`;
- timezone-aware start and end timestamps;
- project, operation, status, environment, session, Story, and surface labels;
- project-relative references and the context-manifest digest;
- identifier-safe failure codes;
- optional boolean, numeric, categorical, or text metrics without comments or
  arbitrary metadata.

Local `input`, `output`, `metadata`, queries, summaries, Score comments, logs,
exceptions, Prompt/Completion fields, source text, absolute paths and secrets
are absent from the remote projection. An unsafe prepared export is rejected
as `PORT_PAYLOAD_UNSAFE` before an adapter or network call.

## Automatic Event Generation

`workflow-observe` builds events from a Story, machine-readable Evidence,
Context Pack selection, Qdrant source references, and the actual Closeout
status. It writes only below
`{data_dir}/production/observability/events`.

```powershell
.\ccgs.cmd workflow-observe `
  --project-root D:\path\to\consumer `
  --story ccgs-data\production\epics\sample\story-001.md `
  --evidence ccgs-data\production\qa\evidence\story-001.json `
  --project-id my-project `
  --event-id story-001-run-001 `
  --trace-key story-001-workflow `
  --session-id sprint-001 `
  --status passed `
  --write
```

The first write for an `event_id` wins. Retries reuse the existing event after
checking project, trace, operation, and Story identity. This preserves stable
Trace, Span, and Score IDs even if a retry happens later.
## Dry Run

Dry-run validates the local event, neutral projection, Integration Port request,
capability and deterministic identities. It emits only the public Port response;
it does not validate vendor configuration, load OpenTelemetry, read credentials,
contact Langfuse, or modify the project.

```powershell
.\ccgs.cmd langfuse-export `
  --project-root D:\path\to\consumer `
  --event ccgs-data\production\observability\events\story-closeout.json `
  --dry-run
```

## Send

Install the optional OpenTelemetry dependencies into the Python environment
used by `ccgs.cmd`:

```powershell
python -m pip install `
  opentelemetry-sdk `
  opentelemetry-exporter-otlp-proto-http
```

Set credentials without placing them in command history:

```powershell
$env:LANGFUSE_PUBLIC_KEY = "pk-lf-..."
$env:LANGFUSE_SECRET_KEY = "sk-lf-..."

.\ccgs.cmd langfuse-export `
  --project-root D:\path\to\consumer `
  --event ccgs-data\production\observability\events\story-closeout.json `
  --host https://cloud.langfuse.com `
  --send
```

Self-hosted Langfuse can use a loopback HTTP host such as
`http://127.0.0.1:3000`. Non-loopback HTTP is rejected unless
`--allow-insecure-http` is explicit.

## Mapping

One neutral event becomes one root OTel span:

- `trace_key` deterministically produces the 32-character trace ID;
- `event_id` deterministically produces the 16-character span ID;
- repeated sends reuse the same IDs;
- operation/status/session/tags become filterable Langfuse attributes;
- project-relative references, failure codes and the context digest remain
  bounded attributes;
- pass/fail/blocked maps to DEFAULT/ERROR/WARNING observation levels;
- explicit metrics reference both the trace ID and observation ID.

Trace export must be acknowledged before Scores are sent. If trace export fails,
no Score call occurs. If a Score call fails after trace success, the command
fails and can be retried with the same stable IDs.

## Suggested Scores

- `context_relevance`: numeric 0-1
- `retrieval_precision`: numeric 0-1
- `decision_correctness`: boolean
- `evidence_coverage`: numeric 0-1
- `closeout_pass`: boolean
- `failure_category`: categorical

These measure workflow quality without claiming access to Codex's private model
telemetry.
