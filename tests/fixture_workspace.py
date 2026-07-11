"""Materialize immutable CCGS fixtures in disposable temporary directories."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures"
PROJECTS_ROOT = FIXTURES_ROOT / "projects"
OVERLAYS_ROOT = FIXTURES_ROOT / "engine-overlays"


class FixtureError(ValueError):
    """Raised when fixture metadata or source immutability is invalid."""


def tree_digest(root: Path) -> str:
    """Return a deterministic digest of every file below a fixture source."""

    digest = hashlib.sha256()
    if not root.is_dir():
        return digest.hexdigest()
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _fixture_dir(root: Path, name: str) -> Path:
    """Resolve one direct fixture child without permitting path traversal."""

    if not name or Path(name).name != name or name in {".", ".."}:
        raise FixtureError(f"invalid fixture name: {name!r}")
    candidate = (root / name).resolve()
    if candidate.parent != root.resolve() or not candidate.is_dir():
        raise FixtureError(f"fixture not found: {name}")
    return candidate


def load_fixture_manifest(root: Path, name: str, expected_kind: str) -> dict[str, object]:
    """Load and validate one fixture manifest."""

    fixture = _fixture_dir(root, name)
    manifest_path = fixture / "fixture.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureError(f"invalid fixture manifest: {manifest_path}") from exc
    if manifest.get("schema_version") != "1.0":
        raise FixtureError(f"unsupported fixture schema: {manifest_path}")
    if manifest.get("kind") != expected_kind or manifest.get("id") != name:
        raise FixtureError(f"fixture identity mismatch: {manifest_path}")
    return manifest


def fixture_catalog() -> dict[str, list[str]]:
    """List the committed lifecycle fixtures and engine overlays."""

    def names(root: Path) -> list[str]:
        return sorted(
            child.name
            for child in root.iterdir()
            if child.is_dir() and (child / "fixture.json").is_file()
        )

    return {
        "projects": names(PROJECTS_ROOT),
        "engine_overlays": names(OVERLAYS_ROOT),
    }


def _copy_payload(source: Path, destination: Path) -> None:
    """Merge a fixture's optional project payload into a temporary workspace."""

    payload = source / "project"
    if payload.is_dir():
        shutil.copytree(payload, destination, dirs_exist_ok=True)


@contextmanager
def materialized_fixture(
    project_fixture: str,
    engine_overlay: str | None = None,
) -> Iterator[Path]:
    """Yield a disposable project composed from lifecycle and engine fixtures."""

    project_source = _fixture_dir(PROJECTS_ROOT, project_fixture)
    load_fixture_manifest(PROJECTS_ROOT, project_fixture, "project")

    sources = [project_source]
    overlay_source: Path | None = None
    if engine_overlay:
        overlay_source = _fixture_dir(OVERLAYS_ROOT, engine_overlay)
        load_fixture_manifest(OVERLAYS_ROOT, engine_overlay, "engine-overlay")
        sources.append(overlay_source)

    source_digests = {source: tree_digest(source) for source in sources}
    try:
        with tempfile.TemporaryDirectory(prefix="ccgs-fixture-") as temp_dir:
            workspace = Path(temp_dir).resolve()
            _copy_payload(project_source, workspace)
            if overlay_source:
                _copy_payload(overlay_source, workspace)
            yield workspace
    finally:
        changed = [
            source
            for source, digest in source_digests.items()
            if tree_digest(source) != digest
        ]
        if changed:
            paths = ", ".join(str(path) for path in changed)
            raise FixtureError(f"fixture sources changed during test: {paths}")
