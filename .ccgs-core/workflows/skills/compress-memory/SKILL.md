---
name: compress-memory
description: "Compresses fine-grained Sprint documents (Stories, closed Epics) into a high-level architecture snapshot to prevent token bloat in future context loading."
argument-hint: "[sprint-number]"
user-invocable: true
allowed-tools: Read, Write, Glob
agent: technical-director
---

# Compress Memory (Snapshot Generation)

This skill summarizes the achievements, architecture changes, and API additions of a completed Sprint into a single snapshot file, allowing older granular files to be ignored in subsequent runs.

## 1. Load Inputs
- Read `ccgs-data/production/sprints/sprint-[N].md` and `ccgs-data/production/sprint-status.yaml`
- Find all `DONE` stories in that Sprint. Read their contents to extract API additions and architectural changes.
- Read current `ccgs-data/project-docs/architecture/architecture.md`.

## 2. Generate Snapshot
Extract the following information:
- **Global Architecture State**: What is the current state of foundation/core layers?
- **Core APIs Implemented**: Interfaces and events exposed during this Sprint.
- **Global Unresolved Tech Debt**: Known hacks or deferred work from the sprint.

Present the snapshot for review.

## 3. Write Snapshot
After approval, write to `ccgs-data/project-docs/architecture/snapshots/architecture-snapshot-[N].md`.
Use the template from `.ccgs-core/docs/templates/architecture-snapshot.md`.

## 4. Archive Old Files
Move completed story files into an `archive` folder, or just mark them as 'cold' so `session-start.sh` skips them.
