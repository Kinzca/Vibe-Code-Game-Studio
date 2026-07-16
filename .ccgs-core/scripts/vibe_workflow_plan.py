#!/usr/bin/env python3
"""Compile a validated, project-neutral workflow manifest into a stable DAG plan."""

from __future__ import annotations

import copy
import hashlib
import heapq
import json
from collections import deque
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


CONTRACT_VERSION = "1.0"

PLAN_DEPENDENCY_NOT_FOUND = "PLAN_DEPENDENCY_NOT_FOUND"
PLAN_SELF_DEPENDENCY = "PLAN_SELF_DEPENDENCY"
PLAN_CYCLE_DETECTED = "PLAN_CYCLE_DETECTED"


@dataclass(frozen=True)
class PlanCompileError(ValueError):
    """A stable DAG compilation failure suitable for machine-readable output.

    Example::

        try:
            compile_plan(execution_manifest)
        except PlanCompileError as error:
            machine_result = error.report()
    """

    code: str
    message: str
    details: Mapping[str, Any]

    def __str__(self) -> str:
        return self.message

    def report(self) -> dict[str, Any]:
        """Return the failure through the versioned plan-result contract.

        Example::

            payload = error.report()
            assert payload["ok"] is False
        """

        return {
            "contract_version": CONTRACT_VERSION,
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": copy.deepcopy(dict(self.details)),
            },
        }


def _manifest_steps(manifest: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    """Return the Schema-validated step sequence supplied by the manifest loader."""

    return manifest["steps"]


def _dependencies(step: Mapping[str, Any]) -> Sequence[str]:
    return step.get("depends_on", ())


def _validate_dependencies(steps: Sequence[Mapping[str, Any]]) -> None:
    step_ids = {step["id"] for step in steps}
    for step in steps:
        for dependency_id in _dependencies(step):
            if dependency_id not in step_ids:
                raise PlanCompileError(
                    PLAN_DEPENDENCY_NOT_FOUND,
                    "workflow plan references an unknown dependency",
                    {"step_id": step["id"], "dependency_id": dependency_id},
                )
    for step in steps:
        if step["id"] in _dependencies(step):
            raise PlanCompileError(
                PLAN_SELF_DEPENDENCY,
                "workflow step may not depend on itself",
                {"step_id": step["id"]},
            )


def _build_graph(
    steps: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, int]]:
    adjacency = {step["id"]: [] for step in steps}
    reverse = {step["id"]: [] for step in steps}
    indegree = {step["id"]: 0 for step in steps}
    for step in steps:
        step_id = step["id"]
        for dependency_id in _dependencies(step):
            adjacency[dependency_id].append(step_id)
            reverse[step_id].append(dependency_id)
            indegree[step_id] += 1
    return adjacency, reverse, indegree


def _topological_order(
    steps: Sequence[Mapping[str, Any]],
    adjacency: Mapping[str, Sequence[str]],
    indegree: dict[str, int],
) -> list[str]:
    declaration_index = {step["id"]: index for index, step in enumerate(steps)}
    ready = [
        (declaration_index[step_id], step_id)
        for step_id, count in indegree.items()
        if count == 0
    ]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        _, step_id = heapq.heappop(ready)
        ordered.append(step_id)
        for dependent_id in adjacency[step_id]:
            indegree[dependent_id] -= 1
            if indegree[dependent_id] == 0:
                heapq.heappush(ready, (declaration_index[dependent_id], dependent_id))
    return ordered


def _reverse_distances(start: str, reverse: Mapping[str, Sequence[str]]) -> dict[str, int]:
    distances = {start: 0}
    pending = deque([start])
    while pending:
        current = pending.popleft()
        for predecessor in reverse[current]:
            if predecessor not in distances:
                distances[predecessor] = distances[current] + 1
                pending.append(predecessor)
    return distances


def _normalize_cycle(cycle: Sequence[str]) -> list[str]:
    open_cycle = list(cycle[:-1])
    first_id = min(open_cycle)
    first_index = open_cycle.index(first_id)
    rotated = open_cycle[first_index:] + open_cycle[:first_index]
    return rotated + [rotated[0]]


def _shortest_cycle_from(
    start: str,
    adjacency: Mapping[str, Sequence[str]],
    reverse: Mapping[str, Sequence[str]],
) -> list[str] | None:
    distances = _reverse_distances(start, reverse)
    candidates = [node for node in adjacency[start] if node in distances]
    if not candidates:
        return None
    cycle_length = min(1 + distances[node] for node in candidates)
    candidates = [node for node in candidates if 1 + distances[node] == cycle_length]

    cycles: list[list[str]] = []
    for first_node in candidates:
        cycle = [start]
        current = first_node
        while current != start:
            cycle.append(current)
            current = min(
                node
                for node in adjacency[current]
                if distances.get(node) == distances[current] - 1
            )
        cycle.append(start)
        cycles.append(_normalize_cycle(cycle))
    return min(cycles, key=lambda item: tuple(item))


def _shortest_cycle(
    step_ids: Sequence[str],
    adjacency: Mapping[str, Sequence[str]],
    reverse: Mapping[str, Sequence[str]],
) -> list[str]:
    cycles = [
        cycle
        for step_id in step_ids
        if (cycle := _shortest_cycle_from(step_id, adjacency, reverse)) is not None
    ]
    return min(cycles, key=lambda item: (len(item) - 1, tuple(item)))


def _plan_id(manifest: Mapping[str, Any], steps: Sequence[Mapping[str, Any]]) -> str:
    identity = {
        "contract_version": CONTRACT_VERSION,
        "schema_version": manifest["schema_version"],
        "steps": steps,
    }
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def compile_plan(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Compile a validated execution manifest without file or process access.

    ``manifest`` must be the successful result of
    ``load_manifest(..., for_execution=True)``. The returned plan preserves the
    declared steps, orders ready nodes by declaration position, and derives its
    identity only from the plan contract version, manifest Schema version, and
    complete step declarations.

    Example::

        manifest = load_manifest(project_root, framework_root, for_execution=True)
        plan = compile_plan(manifest)
        assert plan["step_order"]
    """

    steps = _manifest_steps(manifest)
    _validate_dependencies(steps)
    adjacency, reverse, indegree = _build_graph(steps)
    step_order = _topological_order(steps, adjacency, indegree)
    if len(step_order) != len(steps):
        cycle = _shortest_cycle([step["id"] for step in steps], adjacency, reverse)
        raise PlanCompileError(
            PLAN_CYCLE_DETECTED,
            "workflow plan contains a dependency cycle",
            {"cycle": cycle, "cycle_length": len(cycle) - 1},
        )
    return {
        "contract_version": CONTRACT_VERSION,
        "ok": True,
        "plan_id": _plan_id(manifest, steps),
        "step_order": step_order,
        "steps": copy.deepcopy(list(steps)),
    }
