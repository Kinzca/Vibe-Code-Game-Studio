# CCGS Context Pack

## Summary

- Schema: 1.0
- Story: ccgs-data/production/epics/sample/story-001.md
- Story ID: STORY-001
- Title: Record a deterministic fixture result
- Status: ready
- Sources: 6
- Character budget: 24000

## Source Manifest

| Role | Path | Included | Original | Truncated |
|:---|:---|---:|---:|:---:|
| story | ccgs-data/production/epics/sample/story-001.md | 343 | 343 | no |
| gdd | ccgs-data/design/gdd/core-loop.md | 211 | 211 | no |
| adr | ccgs-data/project-docs/architecture/ADR-0001-deterministic-loop.md | 154 | 154 | no |
| evidence | ccgs-data/production/qa/evidence/story-001.json | 752 | 752 | no |
| evidence | ccgs-data/production/qa/evidence/story-001.md | 107 | 107 | no |
| session | ccgs-data/production/session-state/active.md | 51 | 51 | no |

## Missing References

- None.

## Omitted By Limits

- None.

## Sources

### story: ccgs-data/production/epics/sample/story-001.md

    ---
    id: STORY-001
    title: Record a deterministic fixture result
    status: ready
    gdd_refs:
      - design/gdd/core-loop.md
    adr_refs:
      - project-docs/architecture/ADR-0001-deterministic-loop.md
    ---

    # Acceptance Criteria

    - A context pack can select this Story without reading unrelated project data.
    - QA evidence exists under the synthetic project.

### gdd: ccgs-data/design/gdd/core-loop.md

    # Core Loop GDD

    ## Overview

    This is synthetic design data for fixture tests.

    ## Acceptance Criteria

    - The sample action produces one deterministic result.
    - The result can be referenced by the sample Story.

### adr: ccgs-data/project-docs/architecture/ADR-0001-deterministic-loop.md

    # ADR-0001: Deterministic Fixture Loop

    ## Status

    Accepted

    ## Decision

    The fixture contains no random, network, clock, or external project dependency.

### evidence: ccgs-data/production/qa/evidence/story-001.json

    {
      "schema_version": "1.0",
      "story_id": "STORY-001",
      "result": "pass",
      "acceptance_criteria": [
        {
          "id": "AC-1",
          "status": "pass",
          "evidence": "Context Pack golden test selects only bounded synthetic sources."
        },
        {
          "id": "AC-2",
          "status": "pass",
          "evidence": "Synthetic QA evidence is committed under the fixture evidence root."
        }
      ],
      "checks": [
        {
          "id": "fixture-tests",
          "type": "automated-test",
          "status": "pass",
          "summary": "Repository fixture tests pass without consumer project data."
        },
        {
          "id": "boundary-review",
          "type": "review",
          "status": "pass",
          "summary": "Writes remain inside the explicit CCGS data root."
        }
      ]
    }

### evidence: ccgs-data/production/qa/evidence/story-001.md

    # STORY-001 Test Evidence

    - Fixture type: synthetic
    - Expected result: pass
    - External project data: none

### session: ccgs-data/production/session-state/active.md

    # Active Session

    Current fixture story: STORY-001
