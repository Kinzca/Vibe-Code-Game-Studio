"""Allure implementation of the versioned neutral Reporting Port 1.0."""

from __future__ import annotations

import copy
from typing import Any, Callable, Sequence

from ccgs_allure_adapter import AllureBundle, build_neutral_allure_bundle


CAPABILITY_DOCUMENT = {
    "contract_version": "1.0",
    "adapter_id": "allure-evidence-report-1",
    "capabilities": [{
        "port": "reporting",
        "operation": "export_report",
        "capability": "evidence_report",
        "contract_versions": ["1.0"],
    }],
}

BundleWriter = Callable[[str, AllureBundle], bool]


def allure_capability_document() -> dict[str, Any]:
    """Return an isolated Allure evidence-report Capability Document 1.0."""

    return copy.deepcopy(CAPABILITY_DOCUMENT)


def build_allure_reporting_adapter(
    bundle_writer: BundleWriter,
) -> Callable[[dict[str, Any], float], dict[str, Any]]:
    """Build an adapter that consumes only a validated neutral reporting request.

    The injected writer owns the already-authorized output mapping.  The adapter
    receives no project root, source loader, subprocess service, or state writer.
    """

    if not callable(bundle_writer):
        raise TypeError("bundle_writer must be callable")

    def adapter(request: dict[str, Any], _timeout_seconds: float) -> dict[str, Any]:
        payload = request["payload"]
        bundle = build_neutral_allure_bundle(
            payload["report_id"], payload["results"], payload["evidence"],
        )
        written = bundle_writer(payload["output_ref"], bundle)
        if type(written) is not bool:
            raise TypeError("bundle_writer must return bool")
        data = build_allure_reporting_data(
            request, bundle, reused=not written,
        )
        return _success(request, data)

    return adapter


def build_allure_reporting_data(
    request: dict[str, Any],
    bundle: AllureBundle,
    *,
    reused: bool = False,
    failures: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Project one concrete bundle into neutral Reporting Response Data 1.0."""

    payload = request["payload"]
    output_prefix = payload["output_ref"].rstrip("/")
    rendered_failures = [copy.deepcopy(item) for item in failures]
    return {
        "contract_version": "1.0",
        "outcome": "failed" if rendered_failures else "generated",
        "report_id": payload["report_id"],
        "output_ref": payload["output_ref"],
        "artifact_refs": [
            f"{output_prefix}/{relative}" for relative in sorted(bundle.files)
        ],
        "total_results": bundle.summary["total_results"],
        "status_counts": copy.deepcopy(bundle.summary["statuses"]),
        "reused": reused,
        "failures": rendered_failures,
    }


def _success(request: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract_version": "1.0",
        **{
            key: request[key]
            for key in ("request_id", "project_id", "port", "operation", "capability")
        },
        "ok": True,
        "status": "success",
        "action": "invoke",
        "called": True,
        "data": data,
        "error": None,
    }
