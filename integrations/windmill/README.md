# CCGS Windmill Adapter

This adapter lets a Windmill Windows worker orchestrate CCGS without owning any
game workflow rules. Windmill calls the mounted framework ccgs.cmd entrypoint;
the Batch 4 CLI remains the only component that reads Story or Evidence content
and the only component that writes a Story closeout block.

## Boundary

The adapter permits only these commands:

- doctor --json
- evidence-validate
- closeout --dry-run
- closeout --write

It does not accept arbitrary commands, shell fragments, absolute Story paths,
path traversal, report destinations, test commands, or game source paths.
Arguments are passed as a fixed list to cmd.exe and then ccgs.cmd. The adapter
does not open files under the consumer project.

## Worker Requirements

- A Windmill Windows worker. The adapter intentionally targets ccgs.cmd.
- Python 3.10 or newer available to ccgs.cmd.
- The CCGS framework repository mounted read-only or read-write at a stable path.
- The consumer project mounted at a separate explicit path.
- Write access only when closeout automation should update CCGS-owned Story data.

The worker service account should not receive write permission to runtime source
directories. CCGS write policy remains the final enforcement layer.

## Windmill Assets

The f/ tree contains:

- f/ccgs/story_check.py: read-only Doctor, Evidence, and Closeout inspection.
- f/ccgs/story_closeout.py: inspection followed by closeout --write.
- f/ccgs/story_closeout__flow/flow.yaml: importable Flow with selective retry.
- f/ccgs/folder.meta.yaml: folder declaration required by Windmill sync.

The files ending in .yaml use strict JSON syntax, which is valid YAML 1.2 and
can be validated with the Python standard library.

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

Passing evidence advances review to done through ccgs.cmd. Failed evidence keeps
the current Story state and lets ccgs.cmd update the managed failure block.
Exhausted adapter errors are raised with CCGS_RETRYABLE or CCGS_PERMANENT markers
so the Flow retry_if expression can distinguish them.
