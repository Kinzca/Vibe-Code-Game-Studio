# CCGS Windmill Adapter

This adapter lets a Windmill worker orchestrate CCGS without owning any game
workflow rules. Windows workers call ccgs.cmd; Linux workers call ccgs.sh.
The Batch 4 CLI remains the only component that reads Story or Evidence content
and the only component that writes a Story closeout block.

## Boundary

The adapter permits only these commands:

- doctor --json
- evidence-validate
- closeout --dry-run
- closeout --write
- qdrant-query
- workflow-observe --write
- langfuse-export --dry-run or --send

It does not accept arbitrary commands, shell fragments, absolute Story paths,
path traversal, report destinations, test commands, or game source paths.
Arguments are passed without shell interpolation to the platform entrypoint.
The adapter
does not open files under the consumer project.

## Worker Requirements

- A Windmill Windows worker or an OSS Linux container worker.
- Python 3.10 or newer available to ccgs.cmd or ccgs.sh.
- The CCGS framework repository mounted read-only or read-write at a stable path.
- The consumer project mounted at a separate explicit path.
- Write access only when closeout automation should update CCGS-owned Story data.

The worker service account should not receive write permission to runtime source
directories. CCGS write policy remains the final enforcement layer.

## Windmill Assets

The f/ tree contains:

- f/ccgs/story_check.py: read-only Doctor, Evidence, and Closeout inspection.
- f/ccgs/story_closeout.py: inspection followed by closeout --write.
- f/ccgs/story_closeout__flow/flow.yaml: importable Closeout-only Flow.
- f/ccgs/story_observed_closeout.py: bounded end-to-end orchestration wrapper.
- f/ccgs/story_observed_closeout__flow/flow.yaml: Qdrant, Closeout, event, and Langfuse Flow with selective retry.
- f/ccgs/folder.meta.yaml: folder declaration required by Windmill sync.

The files ending in .yaml use strict JSON syntax, which is valid YAML 1.2 and
can be validated with the Python standard library.

## Observed Closeout Loop

The observed Flow calls only stable `ccgs.cmd` commands in this order:

1. `qdrant-query` retrieves project-scoped references.
2. Doctor, Evidence validation, and Closeout run through the existing adapter.
3. `workflow-observe --write` creates one bounded event under the configured CCGS data root.
4. `langfuse-export --send` sends the Trace first and then the two explicit Scores.

The `event_id` is the retry key. A repeated Flow run reuses the original event,
Trace ID, Span ID, and Score IDs without rewriting its timestamp. Retrieval text
is removed from the Windmill result; only project-relative source references are
forwarded to the event builder.

Set `CCGS_PYTHON` on the worker to a dedicated environment containing
`fastembed`, `opentelemetry-sdk`, and
`opentelemetry-exporter-otlp-proto-http`. Langfuse credentials stay in
`LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`; they are never Flow inputs.
Transient network failures exit with code 3 and are marked `CCGS_RETRYABLE`.
Configuration, credential, Schema, and path failures exit with code 2 and are
not retried.
## Sync

Install the current Windmill CLI separately, then run these commands from this
directory after configuring a workspace:

    wmill workspace add <profile> <workspace-id> <base-url>
    wmill sync push --workspace <profile>

wmill.yaml includes only f/** and skips variables, secrets, resources, apps,
schedules, triggers, users, groups, settings, and workspace keys. Review the
sync preview and configure folder ownership for non-admin workspaces before the
first push.

## Run

Example flow input:

    {
      "framework_root": "E:\\CCGS\\CCGS_Universal_Workflow",
      "project_root": "E:\\Projects\\MyGame",
      "story": "ccgs-data/production/epics/core/story-001.md",
      "evidence": "ccgs-data/production/qa/evidence/story-001.json",
      "apply": true,
      "timeout_seconds": 120
    }

Run the imported flow:

    wmill flow run f/ccgs/story_closeout -d @input.json

Set apply to false for a read-only check. A business failure returns a
structured report and is not retried. A transport or protocol failure is marked
CCGS_RETRYABLE; the Flow retries it twice with a five-second delay. The
standalone scripts instead use the adapter's bounded max_attempts setting.

## Result Contract

Every successful script invocation returns JSON containing:

- status: passed, failed, or error
- ok: true only for a passing closeout
- retryable: true only for transport or protocol errors
- commands: sanitized ccgs.cmd attempt reports
- failures: deduplicated reason codes and messages
- advance: the closeout --write result when apply is true

Passing evidence advances review to done through the stable CCGS CLI. Failed
evidence keeps
the current Story state and lets ccgs.cmd update the managed failure block.
Exhausted adapter errors are raised with CCGS_RETRYABLE or CCGS_PERMANENT markers
so the Flow retry_if expression can distinguish them.
