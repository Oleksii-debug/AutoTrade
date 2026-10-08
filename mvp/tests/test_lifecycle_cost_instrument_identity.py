from datetime import datetime, timedelta, timezone
import unittest

from mvp.autotrade_mvp.lifecycle_cost import (
    LifecycleCostComponent,
    LifecycleCostError,
    LifecycleCostProfile,
    LifecycleCostRequirements,
)


AS_OF = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
HORIZON = AS_OF + timedelta(days=30)


class LifecycleCostInstrumentIdentityTests(unittest.TestCase):
    def profile(self, instrument_version):
        return LifecycleCostProfile(
            profile_id="profile-1",
            decision_scope_ref="decision-scope:test:v1",
            instrument_version=instrument_version,
            as_of=AS_OF,
            horizon_end=HORIZON,
            requirements=LifecycleCostRequirements(requirements_ref="requirements:test:v1", required_kinds=("COMMISSION",)),
            components=(
                LifecycleCostComponent(
                    component_id="commission",
                    phase="TURNOVER",
                    kind="COMMISSION",
                    normalized_rate="0.001",
                    evidence_ref="evidence:commission",
                    observed_at=AS_OF,
                    valid_until=HORIZON,
                ),
            ),
        )

    def test_positive_canonical_version_is_retained_exactly(self):
        identity = "11111111-1111-4111-8111-111111111111@12"
        profile = self.profile(identity)
        self.assertEqual(profile.instrument_version, identity)

    def test_uuid_spelling_is_normalized_to_canonical_common_id(self):
        profile = self.profile("11111111111141118111111111111111@12")
        self.assertEqual(
            profile.instrument_version,
            "11111111-1111-4111-8111-111111111111@12",
        )

    def test_unversioned_or_noncanonical_identity_fails_closed(self):
        for value in (
            "instrument-id",
            "@1",
            "instrument-id@1",
            "11111111-1111-4111-8111-111111111111@",
            "11111111-1111-4111-8111-111111111111@0",
            "11111111-1111-4111-8111-111111111111@01",
            "11111111-1111-4111-8111-111111111111@-1",
            "11111111-1111-4111-8111-111111111111@١",
            "11111111-1111-4111-8111-111111111111@1@2",
            " 11111111-1111-4111-8111-111111111111@1",
            "11111111-1111-4111-8111-111111111111@1 ",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    LifecycleCostError,
                    "canonical instrument-id@version",
                ):
                    self.profile(value)

    def test_hostile_text_subclass_is_rejected_before_string_dispatch(self):
        calls = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                calls.append("strip")
                raise AssertionError("hostile strip executed")

            def split(self, *args, **kwargs):
                calls.append("split")
                raise AssertionError("hostile split executed")

        with self.assertRaises(TypeError):
            self.profile(
                HostileText("11111111-1111-4111-8111-111111111111@1")
            )
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
