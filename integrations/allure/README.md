# Vibe Code Game Studio Allure Reporting Adapter

This adapter implements the versioned
`reporting/export_report/evidence_report` Port. It converts bounded neutral test
results and the neutral Evidence projection into deterministic Allure result
files. It does not execute tests, invoke Allure, inspect project source, access
credentials, or edit workflow state.

## Recommended command

Use `report-export` for new integrations:

```powershell
.\ccgs.cmd report-export `
  --project-root D:\path\to\consumer `
  --project-id example-project `
  --story ccgs-data\production\epics\sample\story-001.md `
  --evidence ccgs-data\production\qa\evidence\story-001.json `
  --test-result ccgs-data\production\qa\test-results\logic.json `
  --test-result ccgs-data\production\qa\test-results\integration.xml `
  --report-id build-20260711-001 `
  --dry-run
```

`allure-export` remains only as a compatibility alias for `report-export`; it
uses the same Reporting Port and does not retain a second rendering or write
path. Replace `--dry-run` with `--write` to publish the immutable bundle under:

```text
ccgs-data/production/qa/reports/<report-id>/
```

Deprecated vendor metadata options are accepted by the command parser only for
compatibility. Non-empty `--engine`, `--environment`, `--build-name`,
`--build-url`, `--report-url`, `--build-order`, or non-zero `--start-ms` values
are rejected with a stable request error. Vendor metadata is not part of the
neutral Reporting Port.

## Input boundary

The trusted core loader reads only explicitly declared normalized JSON or JUnit
files below `production/qa/test-results` and the declared Evidence JSON below
`production/qa/evidence`. It validates and projects those files before invoking
the concrete adapter.

The adapter receives only Reporting Request Data 1.0: normalized result records,
the bounded Evidence projection, a stable report ID, and an authorized relative
output reference. It never receives a project root, source loader, command
runner, state machine, credentials, raw logs, exception text, prompts,
completions, or source code.

## Output model

The deterministic bundle contains only:

- one neutral Allure `*-result.json` for each normalized test result;
- one neutral Evidence `*-result.json` and its bounded JSON attachment;
- `categories.json`.

The adapter does not generate `environment.properties`, `executor.json`, HTML,
vendor links, engine labels, arbitrary attachments, stdout, stderr, or
tracebacks. File identities, ordering, status counts, and content depend only on
the normalized request.

Dry-run performs the same version, path, schema, safety, identity, bundle
planning, and conflict preflight without invoking the adapter or writing files.
It returns the exact deterministic `artifact_refs` that the same concrete bundle
planner supplies to write mode below the bound `output_ref`.

Publishing uses a sibling staging directory and an atomic directory rename.
Replaying identical content returns `reused=true` without changing bytes,
mtimes, or directory membership. Different content at the same report ID is
rejected without overwriting the existing report.

## Optional external HTML generation

HTML generation is outside this adapter. If required, an operator may install an
official Allure CLI separately and manually point it at one immutable report
directory:

```powershell
allure generate `
  ccgs-data\production\qa\reports\build-20260711-001 `
  --output ccgs-data\production\qa\allure-html\build-20260711-001 `
  --clean
```

This external command is not executed by `report-export`, `allure-export`, the
Reporting Port, or the adapter. HTML publication and history retention remain
external CI or operator responsibilities.

## Security and failure behavior

- `--project-root` is mandatory at the trusted CLI boundary and never enters the
  public Port payload.
- Declared result and Evidence files must remain under their configured data
  directories.
- Output is fixed below `production/qa/reports/<report-id>`.
- Report IDs and all artifact references reject traversal, absolute paths,
  backslashes, file URIs, and shell metacharacters.
- Schema, content, conflict, protocol, and business failures are non-retryable;
  only a real post-invocation transport outage or timeout may be retryable.
- Reporting failure never modifies core Result, Evidence, Replay, Closeout, or
  Workflow Event state and never blocks an already completed local Closeout.
