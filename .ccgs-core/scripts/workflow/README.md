# CCGS Workflow Scripts

Use these scripts from the project root. They are designed to keep context usage
small before an agent starts reading documents.

## Context Router

```bash
python3 .ccgs-core/scripts/workflow/ccgs-context-router.py "任务描述"
```

The router returns a short list of recommended files, reasons, and read
commands. Prefer this before broad reads of GDDs, epics, QA evidence, or role
documents.

## Context Cache

Build a compact machine-readable index:

```bash
python3 .ccgs-core/scripts/workflow/ccgs-context-index.py --write
```

Generate the current low-cost startup memo:

```bash
python3 .ccgs-core/scripts/workflow/ccgs-current-context.py --write
```

Generate a story-specific context pack before readiness/dev/done work:

```bash
python3 .ccgs-core/scripts/workflow/ccgs-story-context.py CCGS-Data/production/epics/example/story-001-example.md --write
```

Default behavior prints to stdout. Add `--write` only when you want to persist
the generated cache under `CCGS-Data/production/context/`.

## Session-State Archive

```bash
python3 .ccgs-core/scripts/workflow/archive-session-state.py --keep 10 --brief
```

The archive tool keeps the newest top-level sections in
`CCGS-Data/production/session-state/active.md` and appends older sections to a
monthly archive file. Use `--dry-run` before the first real archive on a project.
