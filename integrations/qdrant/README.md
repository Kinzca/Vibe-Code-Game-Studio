# CCGS Qdrant Adapter

Batch 5C creates an engine-neutral semantic index for CCGS project knowledge.
The stable `ccgs.cmd` surface discovers, chunks, embeds, synchronizes, and queries
only approved CCGS document roots. It never reads game source.

## Indexed Sources

| Kind | Root | Formats |
|---|---|---|
| Story | `{data_dir}/production/epics` | Markdown |
| GDD | `{data_dir}/design/gdd` | Markdown |
| ADR | `{data_dir}/project-docs/**/ADR-*.md` | Markdown |
| Evidence | `{data_dir}/production/qa/evidence` | Markdown, JSON |
| Context Pack | `{data_dir}/production/context` | Markdown |

JSON Evidence is canonicalized before hashing. Markdown is split by headings,
then by bounded overlapping character windows. Point payloads conform to
`schemas/semantic-index-point.schema.json`.

## Runtime Requirements

Run Qdrant separately. The default endpoint is local:

```powershell
docker run --name ccgs-qdrant -p 6333:6333 `
  -v qdrant_storage:/qdrant/storage `
  qdrant/qdrant
```

Install the optional local embedding provider into the Python environment used
by `ccgs.cmd`:

```powershell
python -m pip install fastembed
```

The default model is `sentence-transformers/all-MiniLM-L6-v2`. FastEmbed may
download and cache the model the first time a write or query runs. Dry-run does
not import FastEmbed, download a model, or contact Qdrant.

## Build And Synchronize

Always inspect the deterministic offline plan first:

```powershell
.\ccgs.cmd qdrant-index `
  --project-root D:\path\to\consumer `
  --project-id my-game `
  --dry-run
```

Synchronize after reviewing source counts, chunk counts, and the manifest hash:

```powershell
.\ccgs.cmd qdrant-index `
  --project-root D:\path\to\consumer `
  --project-id my-game `
  --qdrant-url http://127.0.0.1:6333 `
  --write
```

For Qdrant Cloud or another remote host, use HTTPS and place the key in an
environment variable:

```powershell
$env:QDRANT_API_KEY = "..."
.\ccgs.cmd qdrant-index `
  --project-root D:\path\to\consumer `
  --project-id my-game `
  --qdrant-url https://example.cloud.qdrant.io `
  --write
```

The key is never accepted as a command-line value and is not printed. Plain
HTTP for a non-loopback host is rejected unless `--allow-insecure-http` is
explicitly supplied.

## Incremental Contract

Each point ID is UUIDv5 of `project_id + source_path + chunk_index`. The payload
stores source, content, model, and record hashes. A write performs:

1. project-filtered scroll of existing payloads;
2. embedding and upsert of new or changed records only;
3. deletion of stale IDs only after every upsert succeeds.

An identical second run performs zero embeddings and zero writes. Removing a
source prunes only points with the same `project_id`; other projects in the same
collection are untouched. Changing the embedding model re-embeds all desired
points while retaining stable point IDs.

## Query

```powershell
.\ccgs.cmd qdrant-query `
  --project-root D:\path\to\consumer `
  --project-id my-game `
  --query "What ADR governs deterministic save reconstruction?" `
  --limit 10
```

Queries are filtered by `project_id` and return only score, source kind, relative
source path, heading, chunk index, and text. Vectors and unrelated payload keys
are not returned.

## Boundaries

- no game source roots are scanned;
- no index or model cache is written into the consumer project;
- absolute project paths are never stored in payloads or reports;
- source files are UTF-8, size-bounded, and resolved beneath CCGS data roots;
- malformed Evidence JSON, invalid identifiers, vector shape mismatches, and
  collection dimension mismatches fail closed;
- Qdrant updates are retry-safe, but a remote service cannot provide a single
  transaction across every batch, so upserts happen before any stale deletion.

Qdrant concepts used by the adapter are documented in the official
[collections](https://qdrant.tech/documentation/concepts/collections/),
[points](https://qdrant.tech/documentation/concepts/points/), and
[filtering](https://qdrant.tech/documentation/concepts/filtering/) guides.