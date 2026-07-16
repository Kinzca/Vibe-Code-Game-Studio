"""Integration evidence for STORY-UWA-014 upgrade and migration contracts."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / ".ccgs-core" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ccgs_codex_bridge import BRIDGE_VERSION, codex_target_paths
from vibe_project_manifest import MANIFEST_SCHEMA_UNSUPPORTED, ManifestError, load_manifest
from vibe_upgrade import (
    CONTRACT_VERSION,
    MAX_DOCUMENT_BYTES,
    MigrationRegistry,
    MigrationStep,
    UpgradeError,
    apply_upgrade,
    atomic_replace_file,
    build_upgrade_plan,
    load_installation_receipt,
)


CLI = ROOT / ".ccgs-core" / "scripts" / "ccgs_cli.py"
RECEIPT = "ccgs-data/production/upgrade/installation.json"


def project_tree(root: Path) -> tuple[tuple[str, str, int, int], ...]:
    """Capture bytes, mode, mtime and directory membership without mutation."""

    root_metadata = root.stat()
    rows: list[tuple[str, str, int, int]] = [
        (".", "directory", root_metadata.st_mode & 0o777, root_metadata.st_mtime_ns)
    ]
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if path.is_symlink():
            digest = "symlink:" + os.readlink(path)
        elif path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            digest = "directory"
        rows.append((relative, digest, metadata.st_mode & 0o777, metadata.st_mtime_ns))
    return tuple(rows)


def doctor(errors: int = 0) -> dict[str, Any]:
    """Return the bounded portion consumed from the real Doctor contract."""

    return {"summary": {"error": errors}}


class ConsumerProject:
    """One neutral disposable project fixture."""

    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        (self.root / "ccgs-data").mkdir()

    def __enter__(self) -> Path:
        return self.root

    def __exit__(self, *args: object) -> None:
        self._temporary.cleanup()


class UpgradeMigrationPreviewTest(unittest.TestCase):
    """The upgrade surface must be explicit, deterministic and reversible."""

    def plan(self, project: Path, **kwargs: Any) -> dict[str, object]:
        return build_upgrade_plan(ROOT, project, "ccgs-data", "0.8.1", **kwargs)

    def apply(
        self,
        project: Path,
        plan_id: str,
        doctor_fn: Any = None,
        **kwargs: Any,
    ) -> dict[str, object]:
        return apply_upgrade(
            ROOT,
            project,
            "ccgs-data",
            "0.8.1",
            plan_id,
            doctor_fn or (lambda _project, _framework: doctor()),
            **kwargs,
        )

    def test_ac1_version_truth_and_strict_receipt_validation(self) -> None:
        with ConsumerProject() as project:
            plan = self.plan(project)
            self.assertEqual("untracked", plan["compatibility"])
            self.assertEqual("0.8.1", plan["target"]["framework_version"])
            self.assertEqual(BRIDGE_VERSION, plan["target"]["bridge_version"])
            self.assertEqual("1.0", plan["target"]["manifest_schema_version"])
            receipt_path, receipt = load_installation_receipt(project, "ccgs-data")
            self.assertEqual(RECEIPT, receipt_path)
            self.assertIsNone(receipt)

            target = project / RECEIPT
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps({
                "contract_version": "1.0",
                "framework_version": "0.8.1",
                "bridge_version": "1.0",
                "manifest_schema_version": "1.0",
                "managed_files": [{"path": "../escape", "sha256": "0" * 64}],
            }), encoding="utf-8")
            with self.assertRaisesRegex(UpgradeError, "path traversal") as raised:
                self.plan(project)
            self.assertEqual("INSTALLATION_RECEIPT_INVALID", raised.exception.code)

            target.write_bytes(b"{" + b" " * MAX_DOCUMENT_BYTES + b"}")
            with self.assertRaises(UpgradeError) as oversized:
                self.plan(project)
            self.assertEqual("UPGRADE_DOCUMENT_TOO_LARGE", oversized.exception.code)

    def test_ac1_receipt_rejects_extra_fields_windows_paths_and_symlinks(self) -> None:
        base = {
            "contract_version": "1.0", "framework_version": "0.8.1",
            "bridge_version": "1.0", "manifest_schema_version": "1.0",
            "managed_files": [],
        }
        for mutation in (
            {**base, "unexpected": True},
            {**base, "managed_files": [{"path": "C:/unsafe", "sha256": "0" * 64}]},
            {**base, "managed_files": [{"path": "file:unsafe", "sha256": "0" * 64}]},
            {**base, "managed_files": [{"path": "a\\b", "sha256": "0" * 64}]},
        ):
            with self.subTest(mutation=mutation):
                with ConsumerProject() as project:
                    target = project / RECEIPT
                    target.parent.mkdir(parents=True)
                    target.write_text(json.dumps(mutation), encoding="utf-8")
                    with self.assertRaises(UpgradeError):
                        self.plan(project)
        if hasattr(os, "symlink"):
            with ConsumerProject() as project:
                source = project / "receipt.json"
                source.write_text(json.dumps(base), encoding="utf-8")
                target = project / RECEIPT
                target.parent.mkdir(parents=True)
                target.symlink_to(source)
                with self.assertRaises(UpgradeError) as raised:
                    self.plan(project)
                self.assertEqual("INSTALLATION_RECEIPT_INVALID", raised.exception.code)

    def test_ac2_preview_is_deterministic_complete_and_zero_write(self) -> None:
        with ConsumerProject() as project:
            before = project_tree(project)
            first = self.plan(project)
            second = self.plan(project)
            self.assertEqual(first, second)
            self.assertEqual(before, project_tree(project))
            self.assertEqual(
                {
                    "contract_version", "plan_id", "mode", "current", "target",
                    "compatibility", "migrations", "writes", "conflicts", "doctor_required",
                },
                set(first),
            )
            self.assertEqual("dry-run", first["mode"])
            self.assertEqual(64, len(first["plan_id"]))
            self.assertEqual(sorted(item["path"] for item in first["writes"]), [item["path"] for item in first["writes"]])
            self.assertIn(RECEIPT, [item["path"] for item in first["writes"]])
            self.assertLessEqual(len(json.dumps(first, sort_keys=True, separators=(",", ":")).encode()), MAX_DOCUMENT_BYTES)

    def test_ac2_public_cli_preview_is_byte_stable_and_read_only(self) -> None:
        with ConsumerProject() as project:
            command = [sys.executable, str(CLI), "upgrade", "--project-root", str(project), "--dry-run", "--json"]
            before = project_tree(project)
            first = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
            second = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual(first.stdout, second.stdout)
            self.assertEqual("", first.stderr)
            self.assertEqual(before, project_tree(project))

    def test_ac3_unmanaged_collision_and_managed_drift_fail_closed(self) -> None:
        with ConsumerProject() as project:
            collision = project / ".agents/skills/ccgs-context/SKILL.md"
            collision.parent.mkdir(parents=True)
            collision.write_text("# consumer owned\n", encoding="utf-8")
            before = project_tree(project)
            plan = self.plan(project)
            self.assertEqual("UPGRADE_UNMANAGED_CONFLICT", plan["conflicts"][0]["code"])
            self.assertEqual(before, project_tree(project))

        with ConsumerProject() as project:
            initial = self.plan(project)
            result = self.apply(project, initial["plan_id"])
            self.assertEqual("applied", result["outcome"])
            managed = project / "AGENTS.md"
            managed.write_text(managed.read_text(encoding="utf-8") + "consumer drift\n", encoding="utf-8")
            drifted = self.plan(project)
            self.assertIn(
                {"path": "AGENTS.md", "code": "UPGRADE_MANAGED_DRIFT"},
                drifted["conflicts"],
            )

        with ConsumerProject() as project:
            (project / "agents.md").write_text("case collision\n", encoding="utf-8")
            plan = self.plan(project)
            self.assertIn(
                {"path": "AGENTS.md", "code": "UPGRADE_UNMANAGED_CONFLICT"},
                plan["conflicts"],
            )

    def test_ac3_agents_content_outside_managed_block_is_preserved(self) -> None:
        with ConsumerProject() as project:
            original = "# Consumer Instructions\n\nKeep this exact text.\n"
            (project / "AGENTS.md").write_text(original, encoding="utf-8")
            plan = self.plan(project)
            result = self.apply(project, plan["plan_id"])
            self.assertEqual("applied", result["outcome"])
            content = (project / "AGENTS.md").read_text(encoding="utf-8")
            self.assertTrue(content.startswith(original))
            self.assertEqual(1, content.count("CCGS CODEX BRIDGE:BEGIN"))
            self.assertEqual(1, content.count("CCGS CODEX BRIDGE:END"))

    def test_ac3_malformed_agents_markers_return_a_stable_conflict(self) -> None:
        with ConsumerProject() as project:
            (project / "AGENTS.md").write_text(
                "<!-- CCGS CODEX BRIDGE:BEGIN -->\n", encoding="utf-8"
            )
            plan = self.plan(project)
            self.assertEqual(
                [{"path": "AGENTS.md", "code": "UPGRADE_UNMANAGED_CONFLICT"}],
                plan["conflicts"],
            )

    def test_ac3_symlinked_target_parent_is_rejected_before_planning(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symbolic links are unavailable")
        with ConsumerProject() as project, tempfile.TemporaryDirectory() as external:
            (project / ".agents").symlink_to(Path(external), target_is_directory=True)
            plan = self.plan(project)
            self.assertIn(
                {
                    "path": ".agents/skills/ccgs-context/SKILL.md",
                    "code": "UPGRADE_UNMANAGED_CONFLICT",
                },
                plan["conflicts"],
            )
            self.assertEqual([], list(Path(external).iterdir()))

        with ConsumerProject() as project, tempfile.TemporaryDirectory() as external:
            calls = 0

            def swapping_doctor(_project: Path, _framework: Path) -> dict[str, Any]:
                nonlocal calls
                calls += 1
                if calls == 1:
                    (project / ".agents").symlink_to(Path(external), target_is_directory=True)
                return doctor()

            plan = self.plan(project)
            result = self.apply(project, plan["plan_id"], swapping_doctor)
            self.assertEqual("failed", result["outcome"])
            self.assertEqual("UPGRADE_PLAN_STALE", result["failures"][0]["code"])
            self.assertEqual([], list(Path(external).iterdir()))

        with ConsumerProject() as project, tempfile.TemporaryDirectory() as external:
            project = project.resolve()
            parent = project / "atomic-parent"
            parent.mkdir()
            original_parent = project / "atomic-parent-original"

            def swap_parent(_target: Path) -> None:
                parent.rename(original_parent)
                parent.symlink_to(Path(external), target_is_directory=True)

            with self.assertRaises(UpgradeError) as stale:
                atomic_replace_file(
                    parent / "target.txt",
                    b"confined\n",
                    0o600,
                    before_commit=swap_parent,
                )
            self.assertEqual("UPGRADE_PLAN_STALE", stale.exception.code)
            self.assertEqual([], list(Path(external).iterdir()))
            self.assertFalse((original_parent / "target.txt").exists())
            self.assertEqual([], list(original_parent.glob("*.tmp")))

        with ConsumerProject() as project, tempfile.TemporaryDirectory() as external:
            project = project.resolve()
            before = project_tree(project)
            raced = False

            def replace_with_parent_race(path: Path, content: bytes, mode: int) -> None:
                nonlocal raced
                if raced:
                    atomic_replace_file(path, content, mode)
                    return
                raced = True
                parent = project / ".agents"
                detached = project / ".agents-race-original"

                def swap_parent(_target: Path) -> None:
                    parent.rename(detached)
                    parent.symlink_to(Path(external), target_is_directory=True)

                try:
                    atomic_replace_file(path, content, mode, before_commit=swap_parent)
                except Exception:
                    parent.unlink()
                    detached.rename(parent)
                    raise

            plan = self.plan(project)
            result = self.apply(
                project,
                plan["plan_id"],
                replace_file=replace_with_parent_race,
            )
            self.assertEqual("failed", result["outcome"])
            self.assertEqual("UPGRADE_PLAN_STALE", result["failures"][0]["code"])
            self.assertEqual(before, project_tree(project))
            self.assertEqual([], list(Path(external).iterdir()))

    def test_ac4_apply_requires_fresh_plan_and_replay_is_idempotent(self) -> None:
        with ConsumerProject() as project:
            plan = self.plan(project)
            stale = self.apply(project, "0" * 64)
            self.assertEqual("failed", stale["outcome"])
            self.assertEqual("UPGRADE_PLAN_STALE", stale["failures"][0]["code"])
            self.assertEqual(("ccgs-data",), tuple(item.name for item in project.iterdir()))

            applied = self.apply(project, plan["plan_id"])
            self.assertEqual("applied", applied["outcome"])
            self.assertTrue(applied["written"])
            self.assertEqual(sorted(applied["applied_writes"]), applied["applied_writes"])
            self.assertEqual(RECEIPT, applied["applied_writes"][-1])

            receipt_before = (project / RECEIPT).read_bytes()
            mtimes = {path: (project / path).stat().st_mtime_ns for path in (*codex_target_paths(), RECEIPT)}
            replay_plan = self.plan(project)
            replay = self.apply(project, replay_plan["plan_id"])
            self.assertEqual("reused", replay["outcome"])
            self.assertTrue(replay["reused"])
            self.assertFalse(replay["written"])
            self.assertEqual(receipt_before, (project / RECEIPT).read_bytes())
            self.assertEqual(mtimes, {path: (project / path).stat().st_mtime_ns for path in (*codex_target_paths(), RECEIPT)})
            self.assertEqual([], list(project.rglob("*.tmp")))

    def test_ac4_public_cli_apply_uses_expected_plan_id(self) -> None:
        with ConsumerProject() as project:
            preview = subprocess.run(
                [sys.executable, str(CLI), "upgrade", "--project-root", str(project), "--dry-run", "--json"],
                cwd=ROOT, capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, preview.returncode, preview.stderr)
            plan_id = json.loads(preview.stdout)["plan_id"]
            applied = subprocess.run(
                [
                    sys.executable, str(CLI), "upgrade", "--project-root", str(project),
                    "--apply", "--expected-plan-id", plan_id, "--json",
                ],
                cwd=ROOT, capture_output=True, text=True, check=False,
            )
            result = json.loads(applied.stdout)
            self.assertEqual(0, applied.returncode, applied.stderr)
            self.assertEqual("applied", result["outcome"])
            self.assertTrue((project / RECEIPT).is_file())
            self.assertNotIn(str(project), applied.stdout)

    def test_ac4_post_doctor_failure_rolls_back_bytes_mtime_mode_and_directories(self) -> None:
        with ConsumerProject() as project:
            agents = project / "AGENTS.md"
            agents.write_text("# Existing\n", encoding="utf-8")
            os.chmod(agents, 0o600)
            before = project_tree(project)
            calls = iter((0, 1))

            def failing_doctor(_project: Path, _framework: Path) -> dict[str, Any]:
                return doctor(next(calls))

            plan = self.plan(project)
            result = self.apply(project, plan["plan_id"], failing_doctor)
            self.assertEqual("failed", result["outcome"])
            self.assertEqual("rolled-back", result["doctor"]["status"])
            self.assertFalse(result["written"])
            self.assertFalse((project / RECEIPT).exists())
            self.assertEqual(before, project_tree(project))
            self.assertEqual([], list(project.rglob("*.tmp")))

        with ConsumerProject() as project:
            before = project_tree(project)
            calls = 0

            def fail_second_replace(path: Path, content: bytes, mode: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected replacement failure")
                atomic_replace_file(path, content, mode)

            plan = self.plan(project)
            result = self.apply(
                project,
                plan["plan_id"],
                replace_file=fail_second_replace,
            )
            self.assertEqual("UPGRADE_WRITE_FAILED", result["failures"][0]["code"])
            self.assertEqual(before, project_tree(project))

        with ConsumerProject() as project:
            before = project_tree(project)
            plan = self.plan(project)
            result = self.apply(
                project,
                plan["plan_id"],
                read_back=lambda _path: b"verification-mismatch",
            )
            self.assertEqual("UPGRADE_VERIFY_FAILED", result["failures"][0]["code"])
            self.assertEqual(before, project_tree(project))

    def test_ac5_manifest_error_has_structured_upgrade_entry(self) -> None:
        with ConsumerProject() as project:
            (project / "vibe-workflow.json").write_text(
                json.dumps({"schema_version": "9.0", "steps": []}), encoding="utf-8"
            )
            with self.assertRaises(ManifestError) as raised:
                load_manifest(project, ROOT, for_execution=True)
            self.assertEqual(MANIFEST_SCHEMA_UNSUPPORTED, raised.exception.code)
            self.assertEqual(["1.0"], raised.exception.details["supported_versions"])
            self.assertEqual({"command": "upgrade", "mode": "dry-run"}, raised.exception.details["migration"])
            with self.assertRaises(UpgradeError) as unavailable:
                self.plan(project)
            self.assertEqual("MIGRATION_PATH_UNAVAILABLE", unavailable.exception.code)
            self.assertFalse((project / RECEIPT).exists())

            for command in ("manifest-load", "workflow-request"):
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(CLI),
                        command,
                        "--project-root",
                        str(project),
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                report = json.loads(completed.stdout)
                self.assertEqual(1, completed.returncode)
                self.assertEqual(MANIFEST_SCHEMA_UNSUPPORTED, report["error"]["code"])
                self.assertEqual(
                    {"command": "upgrade", "mode": "dry-run"},
                    report["error"]["details"]["migration"],
                )

        for content in (
            json.dumps({"schema_version": "1.0", "steps": "not-an-array"}),
            '{"schema_version":"9.0","steps":NaN}',
        ):
            with self.subTest(content=content):
                with ConsumerProject() as project:
                    manifest = project / "vibe-workflow.json"
                    manifest.write_text(content, encoding="utf-8")
                    before = project_tree(project)
                    with self.assertRaises(UpgradeError) as invalid:
                        self.plan(project)
                    self.assertEqual("MANIFEST_SCHEMA_INVALID", invalid.exception.code)
                    self.assertNotIn(str(project), str(invalid.exception))
                    self.assertEqual(before, project_tree(project))

                    completed = subprocess.run(
                        [
                            sys.executable,
                            str(CLI),
                            "upgrade",
                            "--project-root",
                            str(project),
                            "--dry-run",
                            "--json",
                        ],
                        cwd=ROOT,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    report = json.loads(completed.stdout)
                    self.assertEqual(1, completed.returncode)
                    self.assertEqual("MANIFEST_SCHEMA_INVALID", report["error"]["code"])
                    self.assertNotIn(str(project), completed.stdout)
                    self.assertEqual(before, project_tree(project))

    def test_ac5_registered_migrations_are_consecutive_pure_and_deterministic(self) -> None:
        registry = MigrationRegistry((
            MigrationStep("manifest-0.8-0.9", "manifest", "0.8", "0.9", True, lambda value: {**value, "schema_version": "0.9"}),
            MigrationStep("manifest-0.9-1.0", "manifest", "0.9", "1.0", True, lambda value: {**value, "schema_version": "1.0"}),
        ))
        path = registry.path("manifest", "0.8", "1.0")
        source = {"schema_version": "0.8", "steps": []}
        first = registry.apply(source, path)
        second = registry.apply(source, path)
        self.assertEqual("1.0", first["schema_version"])
        self.assertEqual(first, second)
        self.assertEqual("0.8", source["schema_version"])
        with self.assertRaises(UpgradeError) as missing:
            registry.path("manifest", "7.0", "1.0")
        self.assertEqual("MIGRATION_PATH_UNAVAILABLE", missing.exception.code)
        with self.assertRaises(UpgradeError):
            MigrationRegistry((
                MigrationStep("duplicate", "manifest", "0.8", "0.9", True, lambda value: value),
                MigrationStep("duplicate", "manifest", "0.9", "1.0", True, lambda value: value),
            ))
        destructive = MigrationRegistry((
            MigrationStep(
                "destructive", "manifest", "0.8", "1.0", False,
                lambda _value: {"schema_version": "1.0"},
            ),
        ))
        with self.assertRaises(UpgradeError) as discarded:
            destructive.apply(source, destructive.path("manifest", "0.8", "1.0"))
        self.assertEqual("MIGRATION_PATH_UNAVAILABLE", discarded.exception.code)

        cyclic = MigrationRegistry((
            MigrationStep("cycle-a", "manifest", "0.8", "0.9", True, lambda value: {**value, "schema_version": "0.9"}),
            MigrationStep("cycle-b", "manifest", "0.9", "0.8", True, lambda value: {**value, "schema_version": "0.8"}),
        ))
        with self.assertRaises(UpgradeError) as cycle:
            cyclic.path("manifest", "0.8", "1.0")
        self.assertEqual("MIGRATION_PATH_UNAVAILABLE", cycle.exception.code)

        def disclose_path(_value: dict[str, Any]) -> dict[str, Any]:
            raise ValueError("secret /tmp/private")

        for transform in (disclose_path, lambda _value: []):
            with self.subTest(transform=transform):
                invalid = MigrationRegistry((
                    MigrationStep("invalid-output", "manifest", "0.8", "1.0", True, transform),
                ))
                with ConsumerProject() as project:
                    (project / "vibe-workflow.json").write_text(
                        json.dumps({"schema_version": "0.8", "steps": []}),
                        encoding="utf-8",
                    )
                    before = project_tree(project)
                    with self.assertRaises(UpgradeError) as invalid_output:
                        self.plan(project, migrations=invalid)
                    self.assertEqual(
                        "MIGRATION_PATH_UNAVAILABLE",
                        invalid_output.exception.code,
                    )
                    self.assertNotIn("/tmp/private", str(invalid_output.exception))
                    self.assertEqual(before, project_tree(project))

        with ConsumerProject() as project:
            manifest = project / "vibe-workflow.json"
            manifest.write_text(
                json.dumps({"schema_version": "0.8", "steps": []}),
                encoding="utf-8",
            )
            plan = self.plan(project, migrations=registry)
            self.assertEqual("untracked", plan["compatibility"])
            self.assertEqual(
                ["manifest-0.8-0.9", "manifest-0.9-1.0"],
                [item["id"] for item in plan["migrations"]],
            )
            self.assertIn("vibe-workflow.json", [item["path"] for item in plan["writes"]])
            result = self.apply(project, plan["plan_id"], migrations=registry)
            self.assertEqual("applied", result["outcome"])
            self.assertEqual("1.0", json.loads(manifest.read_text())["schema_version"])
            receipt = json.loads((project / RECEIPT).read_text())
            self.assertEqual("1.0", receipt["manifest_schema_version"])
            self.assertNotIn(
                "vibe-workflow.json",
                [item["path"] for item in receipt["managed_files"]],
            )

    def test_ac6_doctor_failure_is_non_retryable_and_writes_nothing(self) -> None:
        with ConsumerProject() as project:
            for relative in (
                "ccgs-data/production/qa/evidence/sentinel.json",
                "ccgs-data/production/replay/sentinel.json",
                "ccgs-data/production/closeout/sentinel.json",
                "ccgs-data/production/observability/events/sentinel.json",
                "ccgs-data/production/plans/sentinel.json",
                "ccgs-data/production/results/sentinel.json",
            ):
                target = project / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('{"sentinel":true}\n', encoding="utf-8")
            framework_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            before = project_tree(project)
            plan = self.plan(project)
            result = self.apply(project, plan["plan_id"], lambda _project, _framework: doctor(2))
            self.assertEqual("failed", result["outcome"])
            self.assertEqual("failed", result["doctor"]["status"])
            self.assertEqual(2, result["doctor"]["before_errors"])
            self.assertFalse(result["failures"][0]["retryable"])
            self.assertEqual(before, project_tree(project))
            self.assertFalse((project / RECEIPT).exists())
            self.assertEqual(
                framework_head,
                subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout,
            )

    def test_ac6_doctor_exception_is_normalized_without_path_disclosure(self) -> None:
        with ConsumerProject() as project:
            plan = self.plan(project)
            result = self.apply(
                project,
                plan["plan_id"],
                lambda _project, _framework: doctor(-1),
            )
            self.assertEqual("failed", result["outcome"])
            self.assertEqual("UPGRADE_DOCTOR_FAILED", result["failures"][0]["code"])
            self.assertEqual(0, result["doctor"]["before_errors"])

        with ConsumerProject() as project:
            plan = self.plan(project)

            def unavailable(_project: Path, _framework: Path) -> dict[str, Any]:
                raise ValueError(f"private path: {project}")

            result = self.apply(project, plan["plan_id"], unavailable)
            self.assertEqual("UPGRADE_DOCTOR_FAILED", result["failures"][0]["code"])
            self.assertNotIn(str(project), json.dumps(result))
            self.assertFalse(result["failures"][0]["retryable"])

        with ConsumerProject() as project:
            initial = self.plan(project)
            self.assertEqual("applied", self.apply(project, initial["plan_id"])["outcome"])
            replay = self.plan(project)
            calls = 0

            def fail_second_doctor(_project: Path, _framework: Path) -> dict[str, Any]:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError(f"private path: {project}")
                return doctor()

            result = self.apply(project, replay["plan_id"], fail_second_doctor)
            self.assertEqual("failed", result["outcome"])
            self.assertEqual("UPGRADE_DOCTOR_FAILED", result["failures"][0]["code"])
            self.assertNotIn(str(project), json.dumps(result))

    def test_ac6_public_contract_schemas_are_versioned_and_strict(self) -> None:
        for name in (
            "installation-receipt.schema.json",
            "upgrade-plan.schema.json",
            "upgrade-result.schema.json",
        ):
            with self.subTest(name=name):
                schema = json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))
                self.assertEqual("https://json-schema.org/draft/2020-12/schema", schema["$schema"])
                self.assertFalse(schema["additionalProperties"])
        self.assertEqual(CONTRACT_VERSION, "1.0")


if __name__ == "__main__":
    unittest.main()
