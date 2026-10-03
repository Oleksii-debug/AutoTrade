import unittest

from mvp.autotrade_mvp.bybit_credential_nonacceptance import (
    BybitCredentialNonAcceptance,
    classify_bybit_credential_nonacceptance,
)
from mvp.autotrade_mvp.provider_core import ProviderCoreError


class _IntSubclass(int):
    pass


class _StrSubclass(str):
    pass


class BybitCredentialNonAcceptanceTests(unittest.TestCase):
    def test_success_means_old_credential_is_still_accepted(self):
        for family in (
            "SPOT",
            "MARGIN",
            "LINEAR_DERIVATIVES",
            "INVERSE_DERIVATIVES",
            "OPTIONS",
        ):
            with self.subTest(family=family):
                self.assertIs(
                    classify_bybit_credential_nonacceptance(
                        ret_code=0,
                        product_family=family,
                    response_surface="V5_UTA_REST",
                    ),
                    BybitCredentialNonAcceptance.STILL_ACCEPTED,
                )

    def test_invalid_key_is_rejection_only_for_separately_bound_exact_domain(self):
        self.assertIs(
            classify_bybit_credential_nonacceptance(
                ret_code=10003,
                product_family="SPOT",
            response_surface="V5_UTA_REST",
            ),
            BybitCredentialNonAcceptance.REJECTED_EXACT_DOMAIN,
        )

    def test_spot_expiry_is_not_relabelled_as_derivatives_rejection(self):
        for family in ("SPOT", "MARGIN"):
            with self.subTest(family=family):
                self.assertIs(
                    classify_bybit_credential_nonacceptance(
                        ret_code=-2015,
                        product_family=family,
                    response_surface="V5_UTA_REST",
                    ),
                    BybitCredentialNonAcceptance.REJECTED_EXACT_DOMAIN,
                )
        for family in (
            "LINEAR_DERIVATIVES",
            "INVERSE_DERIVATIVES",
            "OPTIONS",
        ):
            with self.subTest(family=family):
                self.assertIs(
                    classify_bybit_credential_nonacceptance(
                        ret_code=-2015,
                        product_family=family,
                    response_surface="V5_UTA_REST",
                    ),
                    BybitCredentialNonAcceptance.INCONCLUSIVE,
                )

    def test_derivatives_expiry_is_not_relabelled_as_spot_or_options_rejection(self):
        for family in ("LINEAR_DERIVATIVES", "INVERSE_DERIVATIVES"):
            with self.subTest(family=family):
                self.assertIs(
                    classify_bybit_credential_nonacceptance(
                        ret_code=33004,
                        product_family=family,
                    response_surface="V5_UTA_REST",
                    ),
                    BybitCredentialNonAcceptance.REJECTED_EXACT_DOMAIN,
                )
        for family in ("SPOT", "MARGIN", "OPTIONS"):
            with self.subTest(family=family):
                self.assertIs(
                    classify_bybit_credential_nonacceptance(
                        ret_code=33004,
                        product_family=family,
                    response_surface="V5_UTA_REST",
                    ),
                    BybitCredentialNonAcceptance.INCONCLUSIVE,
                )

    def test_bad_signature_permission_auth_and_ip_codes_are_inconclusive(self):
        for code in (10004, 10005, 10007, 10010):
            with self.subTest(code=code):
                self.assertIs(
                    classify_bybit_credential_nonacceptance(
                        ret_code=code,
                        product_family="SPOT",
                    response_surface="V5_UTA_REST",
                    ),
                    BybitCredentialNonAcceptance.INCONCLUSIVE,
                )

    def test_http_only_or_unknown_provider_failure_is_inconclusive(self):
        for code in (None, 429, 10000, 10016, 99999):
            with self.subTest(code=code):
                self.assertIs(
                    classify_bybit_credential_nonacceptance(
                        ret_code=code,
                        product_family="SPOT",
                    response_surface="V5_UTA_REST",
                    ),
                    BybitCredentialNonAcceptance.INCONCLUSIVE,
                )

    def test_websocket_10003_cannot_be_relabelled_as_api_key_rejection(self):
        with self.assertRaisesRegex(
            ProviderCoreError,
            "V5_UTA_REST",
        ):
            classify_bybit_credential_nonacceptance(
                ret_code=10003,
                product_family="SPOT",
                response_surface="WS_OE_GENERAL",
            )

    def test_noncanonical_or_hostile_response_surface_is_rejected(self):
        for surface in ("", "V5_UTA_REST ", "v5_uta_rest", "REST"):
            with self.subTest(surface=surface), self.assertRaises(ProviderCoreError):
                classify_bybit_credential_nonacceptance(
                    ret_code=10003,
                    product_family="SPOT",
                    response_surface=surface,
                )
        with self.assertRaises(ProviderCoreError):
            classify_bybit_credential_nonacceptance(
                ret_code=10003,
                product_family="SPOT",
                response_surface=_StrSubclass("V5_UTA_REST"),
            )

    def test_noncanonical_family_is_rejected_instead_of_normalized(self):
        for family in ("spot", " SPOT", "SPOT ", "FUTURES", ""):
            with self.subTest(family=family), self.assertRaises(ProviderCoreError):
                classify_bybit_credential_nonacceptance(
                    ret_code=10003,
                    product_family=family,
                response_surface="V5_UTA_REST",
                )

    def test_hostile_scalar_subclasses_are_not_admitted_as_semantic_authority(self):
        with self.assertRaises(ProviderCoreError):
            classify_bybit_credential_nonacceptance(
                ret_code=_IntSubclass(10003),
                product_family="SPOT",
            response_surface="V5_UTA_REST",
            )
        with self.assertRaises(ProviderCoreError):
            classify_bybit_credential_nonacceptance(
                ret_code=10003,
                product_family=_StrSubclass("SPOT"),
            response_surface="V5_UTA_REST",
            )
        with self.assertRaises(ProviderCoreError):
            classify_bybit_credential_nonacceptance(
                ret_code=True,
                product_family="SPOT",
            response_surface="V5_UTA_REST",
            )


if __name__ == "__main__":
    unittest.main()
