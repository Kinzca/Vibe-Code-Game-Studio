# CCGS Allure Adapter

Batch 5B converts engine-neutral automated test data and Story Closeout Evidence
into standard Allure result files. It does not execute tests, inspect game source,
or edit Story state.

## Inputs

All inputs are scoped to the explicit consumer project:

- Story Markdown under `{data_dir}/production/epics`
- Evidence JSON under `{data_dir}/production/qa/evidence`
- repeatable normalized JSON or JUnit XML files under
  `{data_dir}/production/qa/test-results`

The normalized JSON contract is `schemas/automated-test-results.schema.json`.
JUnit `testsuite` and `testsuites` documents are supported without an
engine-specific parser.

## Export

Preview the exact output manifest first:

```powershell
.\ccgs.cmd allure-export `
  --project-root D:\path\to\consumer `
  --story ccgs-data\production\epics\sample\story-001.md `
  --test-result ccgs-data\production\qa\test-results\logic.json `
  --test-result ccgs-data\production\qa\test-results\integration.xml `
  --run-id build-20260711-001 `
  --engine godot `
  --environment ci `
  --dry-run
```

Replace `--dry-run` with `--write` to atomically create:

```text
ccgs-data/production/qa/allure-results/<run-id>/
```

A run directory is immutable. Repeating the exact export is idempotent; an
existing directory with different content is rejected. Use a new run ID for a
new test execution.

## Result Model

Each automated test becomes one `*-result.json`. Each Story also gets one
`Closeout Evidence` result whose steps represent acceptance criteria and checks,
with the source Evidence JSON attached. The adapter also writes:

- `categories.json` for CCGS evidence and infrastructure failures
- `environment.properties` for run, engine, and environment labels
- `executor.json` for build and report links

`historyId` and `testCaseId` are stable across run IDs; `uuid` is unique to the
run. This lets Allure associate repeated tests while preserving each immutable
execution.

## Generate HTML

Install an official Allure command-line distribution separately, then point it
at one immutable result directory:

```powershell
allure generate `
  ccgs-data\production\qa\allure-results\build-20260711-001 `
  --output ccgs-data\production\qa\allure-report\build-20260711-001 `
  --clean
```

HTML report publication and history retention belong to CI or Windmill. They
must not be implemented by reading or modifying game source.

## Security And Failure Behavior

- `--project-root` is mandatory.
- test inputs outside `production/qa/test-results` are rejected.
- output is fixed below `production/qa/allure-results/<run-id>`.
- run IDs cannot contain separators or traversal segments.
- malformed Evidence, JSON, XML, timestamps, and numeric fields fail closed.
- no generated result contains the absolute consumer project path.