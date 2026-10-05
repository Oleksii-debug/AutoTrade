from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import FunctionType
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore
import mvp.autotrade_mvp.runtime_target_host_resource_evidence as resource_module
from mvp.autotrade_mvp.runtime_target_host_resource_evidence import (
    RuntimeTargetHostResourceEvidenceError,
    run_declared_target_host_campaign_with_resources,
)
from mvp.tests.test_runtime_target_host_resource_evidence import (
    INVENTORY_ARTIFACT_ID,
    MEASUREMENT_ARTIFACT_ID,
    RESOURCE_ARTIFACT_ID,
    RUN_RECEIPT_ARTIFACT_ID,
    _run_result,
)


class RuntimeTargetHostExternalCallableAuthorityTests(unittest.TestCase):
    def test_wrapper_rejects_in_place_external_disk_usage_executable_poisoning(self):
        self.assertIs(type(resource_module._disk_usage), FunctionType)
        original_code = resource_module._disk_usage.__code__

        def malicious_runner(**kwargs):
            # Preserve the exact external function object referenced by the collector,
            # but replace only its executable code after the first resource cut.
            # shutil.disk_usage owns _ntuple_diskusage in its original globals, so
            # the forged code remains capable of returning a valid-shaped result.
            observed = resource_module._disk_usage(kwargs["evidence_store"].root)
            source = (
                "def forged(path):\n"
                f"    return _ntuple_diskusage({observed.total}, "
                f"{observed.used}, {observed.free})\n"
            )
            namespace: dict[str, object] = {}
            exec(compile(source, "<forged-disk-usage>", "exec"), namespace, namespace)
            forged = namespace["forged"]
            resource_module._disk_usage.__code__ = forged.__code__
            return _run_result()

        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(Path(root) / "evidence")
            try:
                with patch(
                    "mvp.autotrade_mvp.runtime_target_host_resource_evidence.run_declared_target_host_campaign",
                    side_effect=malicious_runner,
                ):
                    with self.assertRaisesRegex(
                        RuntimeTargetHostResourceEvidenceError,
                        "resource platform dependency",
                    ):
                        run_declared_target_host_campaign_with_resources(
                            journal=object(),
                            evidence_store=store,
                            spec=object(),
                            authority_id="resource-authority",
                            research_plan_id="resource-research-plan",
                            financial_operations={},
                            research_operations={},
                            inventory_artifact_id=INVENTORY_ARTIFACT_ID,
                            measurement_artifact_id=MEASUREMENT_ARTIFACT_ID,
                            run_receipt_artifact_id=RUN_RECEIPT_ARTIFACT_ID,
                            resource_artifact_id=RESOURCE_ARTIFACT_ID,
                        )
            finally:
                resource_module._disk_usage.__code__ = original_code

            # Assert absence while the ArtifactStore root still exists; checking
            # only after TemporaryDirectory cleanup would be trivially green.
            with self.assertRaises(FileNotFoundError):
                store.read_authenticated_snapshot(RESOURCE_ARTIFACT_ID)

    def test_wrapper_rejects_preentry_in_place_disk_usage_code_poisoning(self):
        self.assertIs(type(resource_module._disk_usage), FunctionType)
        original_code = resource_module._disk_usage.__code__

        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(Path(root) / "evidence")
            observed = resource_module._disk_usage(store.root)
            source = (
                "def forged(path):\n"
                f"    return _ntuple_diskusage({observed.total}, "
                f"{observed.used}, {observed.free})\n"
            )
            namespace: dict[str, object] = {}
            exec(
                compile(source, "<preentry-forged-disk-usage>", "exec"),
                namespace,
                namespace,
            )
            forged = namespace["forged"]
            try:
                resource_module._disk_usage.__code__ = forged.__code__
                with patch(
                    "mvp.autotrade_mvp.runtime_target_host_resource_evidence.run_declared_target_host_campaign",
                    side_effect=AssertionError("runner must not execute"),
                ):
                    with self.assertRaisesRegex(
                        RuntimeTargetHostResourceEvidenceError,
                        "resource platform dependency changed before target-host run: disk usage",
                    ):
                        run_declared_target_host_campaign_with_resources(
                            journal=object(),
                            evidence_store=store,
                            spec=object(),
                            authority_id="resource-authority",
                            research_plan_id="resource-research-plan",
                            financial_operations={},
                            research_operations={},
                            inventory_artifact_id=INVENTORY_ARTIFACT_ID,
                            measurement_artifact_id=MEASUREMENT_ARTIFACT_ID,
                            run_receipt_artifact_id=RUN_RECEIPT_ARTIFACT_ID,
                            resource_artifact_id=RESOURCE_ARTIFACT_ID,
                        )
            finally:
                resource_module._disk_usage.__code__ = original_code

            with self.assertRaises(FileNotFoundError):
                store.read_authenticated_snapshot(RESOURCE_ARTIFACT_ID)

    def test_wrapper_rejects_in_run_disk_usage_statvfs_replacement(self):
        if type(resource_module._disk_usage) is not FunctionType:
            self.skipTest("disk_usage is not a Python function on this platform")
        disk_usage_globals = resource_module._disk_usage.__globals__
        disk_os = disk_usage_globals.get("os")
        if disk_os is None or not hasattr(disk_os, "statvfs"):
            self.skipTest("disk_usage does not use os.statvfs on this platform")
        original_statvfs = disk_os.statvfs

        def forged_statvfs(path):
            return original_statvfs(path)

        def malicious_runner(**_kwargs):
            disk_os.statvfs = forged_statvfs
            return _run_result()

        try:
            with tempfile.TemporaryDirectory() as root:
                store = ArtifactStore(Path(root) / "evidence")
                with patch(
                    "mvp.autotrade_mvp.runtime_target_host_resource_evidence.run_declared_target_host_campaign",
                    side_effect=malicious_runner,
                ):
                    with self.assertRaisesRegex(
                        RuntimeTargetHostResourceEvidenceError,
                        "resource platform dependency changed during target-host run: disk usage os.statvfs",
                    ):
                        run_declared_target_host_campaign_with_resources(
                            journal=object(),
                            evidence_store=store,
                            spec=object(),
                            authority_id="resource-authority",
                            research_plan_id="resource-research-plan",
                            financial_operations={},
                            research_operations={},
                            inventory_artifact_id=INVENTORY_ARTIFACT_ID,
                            measurement_artifact_id=MEASUREMENT_ARTIFACT_ID,
                            run_receipt_artifact_id=RUN_RECEIPT_ARTIFACT_ID,
                            resource_artifact_id=RESOURCE_ARTIFACT_ID,
                        )
                with self.assertRaises(FileNotFoundError):
                    store.read_authenticated_snapshot(RESOURCE_ARTIFACT_ID)
        finally:
            disk_os.statvfs = original_statvfs

    def test_wrapper_rejects_preentry_disk_usage_statvfs_replacement(self):
        if type(resource_module._disk_usage) is not FunctionType:
            self.skipTest("disk_usage is not a Python function on this platform")
        disk_usage_globals = resource_module._disk_usage.__globals__
        disk_os = disk_usage_globals.get("os")
        if disk_os is None or not hasattr(disk_os, "statvfs"):
            self.skipTest("disk_usage does not use os.statvfs on this platform")
        original_statvfs = disk_os.statvfs

        def forged_statvfs(path):
            return original_statvfs(path)

        try:
            disk_os.statvfs = forged_statvfs
            with tempfile.TemporaryDirectory() as root:
                store = ArtifactStore(Path(root) / "evidence")
                with patch(
                    "mvp.autotrade_mvp.runtime_target_host_resource_evidence.run_declared_target_host_campaign",
                    side_effect=AssertionError("runner must not execute"),
                ):
                    with self.assertRaisesRegex(
                        RuntimeTargetHostResourceEvidenceError,
                        "resource platform dependency changed before target-host run: disk usage os.statvfs",
                    ):
                        run_declared_target_host_campaign_with_resources(
                            journal=object(),
                            evidence_store=store,
                            spec=object(),
                            authority_id="resource-authority",
                            research_plan_id="resource-research-plan",
                            financial_operations={},
                            research_operations={},
                            inventory_artifact_id=INVENTORY_ARTIFACT_ID,
                            measurement_artifact_id=MEASUREMENT_ARTIFACT_ID,
                            run_receipt_artifact_id=RUN_RECEIPT_ARTIFACT_ID,
                            resource_artifact_id=RESOURCE_ARTIFACT_ID,
                        )
                with self.assertRaises(FileNotFoundError):
                    store.read_authenticated_snapshot(RESOURCE_ARTIFACT_ID)
        finally:
            disk_os.statvfs = original_statvfs

    def test_wrapper_rejects_preentry_disk_usage_binding_replacement(self):
        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(Path(root) / "evidence")
            with (
                patch.object(resource_module, "_disk_usage", object()),
                patch(
                    "mvp.autotrade_mvp.runtime_target_host_resource_evidence.run_declared_target_host_campaign",
                    side_effect=AssertionError("runner must not execute"),
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeTargetHostResourceEvidenceError,
                    "resource platform dependency changed before target-host run: disk usage",
                ):
                    run_declared_target_host_campaign_with_resources(
                        journal=object(),
                        evidence_store=store,
                        spec=object(),
                        authority_id="resource-authority",
                        research_plan_id="resource-research-plan",
                        financial_operations={},
                        research_operations={},
                        inventory_artifact_id=INVENTORY_ARTIFACT_ID,
                        measurement_artifact_id=MEASUREMENT_ARTIFACT_ID,
                        run_receipt_artifact_id=RUN_RECEIPT_ARTIFACT_ID,
                        resource_artifact_id=RESOURCE_ARTIFACT_ID,
                    )

            with self.assertRaises(FileNotFoundError):
                store.read_authenticated_snapshot(RESOURCE_ARTIFACT_ID)


if __name__ == "__main__":
    unittest.main()
