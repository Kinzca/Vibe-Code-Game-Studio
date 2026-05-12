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

## Session-State Archive

```bash
python3 .ccgs-core/scripts/workflow/archive-session-state.py --keep 10 --brief
```

The archive tool keeps the newest top-level sections in
`CCGS-Data/production/session-state/active.md` and appends older sections to a
monthly archive file. Use `--dry-run` before the first real archive on a project.
