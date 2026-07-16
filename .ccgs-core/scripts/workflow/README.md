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


## Codex Bridge Bootstrap

Preview the project-local AGENTS and Skill write plan before applying it:

    .\ccgs.cmd bootstrap --project-root D:\path\to\consumer --codex --dry-run
    .\ccgs.cmd bootstrap --project-root D:\path\to\consumer --codex --write

Bootstrap preserves content outside the CCGS-managed AGENTS block and refuses
to replace same-name Skills that do not carry the CCGS management marker.

## Session-State Archive

```bash
python3 .ccgs-core/scripts/workflow/archive-session-state.py --keep 10 --brief
```

The archive tool keeps the newest top-level sections in
`ccgs-data/production/session-state/active.md` and appends older sections to a
monthly archive file. Use `--dry-run` before the first real archive on a project.

## Story Automation and Closeout

The public CLI owns state and evidence safety rules:

    .\ccgs.cmd story-advance --project-root D:\path\to\consumer --story ccgs-data\production\epics\example\story-001.md --to review --dry-run
    .\ccgs.cmd evidence-validate --project-root D:\path\to\consumer --evidence ccgs-data\production\qa\evidence\story-001.json
    .\ccgs.cmd closeout --project-root D:\path\to\consumer --story ccgs-data\production\epics\example\story-001.md --dry-run

Use write mode only after inspecting the JSON report. External orchestrators
must call these commands instead of editing Story status or closeout blocks
directly.
