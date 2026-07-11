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

Generate a repository-safe Story Context Pack through the public CLI:

    .\ccgs.cmd context-pack --project-root D:\path\to\consumer --story ccgs-data\production\epics\example\story-001.md
    .\ccgs.cmd context-pack --project-root D:\path\to\consumer --story ccgs-data\production\epics\example\story-001.md --write

Preview is the default. Write mode is restricted to the configured
production/context directory. The older ccgs-story-context.py entrypoint remains
available for compatibility with existing projects.

## Session-State Archive

```bash
python3 .ccgs-core/scripts/workflow/archive-session-state.py --keep 10 --brief
```

The archive tool keeps the newest top-level sections in
`ccgs-data/production/session-state/active.md` and appends older sections to a
monthly archive file. Use `--dry-run` before the first real archive on a project.
