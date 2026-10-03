import unittest

from autotrade_runtime.strict_json import InvalidJsonDomainError
from mvp.autotrade_mvp.runtime_target_host_measurement import (
    RuntimeTargetHostMeasurementError,
    TargetHostMeasurementArtifact,
)


class RuntimeTargetHostStrictJsonBoundsTests(unittest.TestCase):
    def _assert_resource_fence(self, raw: bytes) -> None:
        with self.assertRaises(RuntimeTargetHostMeasurementError) as caught:
            TargetHostMeasurementArtifact.parse(raw)
        self.assertIsInstance(caught.exception.__cause__, InvalidJsonDomainError)

    def test_oversized_document_fails_before_semantic_decode(self) -> None:
        self._assert_resource_fence(b'{"x":"' + (b"a" * 1_000_001) + b'"}')

    def test_excessive_nesting_fails_before_recursive_decode(self) -> None:
        raw = ('{"x":' + ('[' * 129) + '0' + (']' * 129) + '}').encode("utf-8")
        self._assert_resource_fence(raw)

    def test_oversized_integer_fails_before_artifact_field_validation(self) -> None:
        self._assert_resource_fence(b'{"x":' + (b"9" * 641) + b'}')


if __name__ == "__main__":
    unittest.main()
