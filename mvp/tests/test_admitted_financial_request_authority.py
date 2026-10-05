from __future__ import annotations

from collections.abc import Mapping
import copy
import inspect
import pickle
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.admitted_financial_request_authority import (
    AdmittedFinancialRequestAuthority,
    AdmittedFinancialRequestAuthorityError,
    AdmittedFinancialRequestAuthorityIssuer,
)
from mvp.autotrade_mvp.financial_binding_dispatch import financial_submission_scope
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.tests.test_financial_binding_dispatch import FinancialBindingDispatchTests


class _ExplosiveMapping(Mapping):
    def __init__(self) -> None:
        self.calls = 0

    def _explode(self):
        self.calls += 1
        raise AssertionError("caller mapping callback executed")

    def __getitem__(self, _key):
        return self._explode()

    def __iter__(self):
        return self._explode()

    def __len__(self):
        return self._explode()


class _ExplosiveDict(dict):
    def __init__(self, value) -> None:
        dict.__init__(self, value)
        self.calls = 0

    def _explode(self):
        self.calls += 1
        raise AssertionError("caller dict callback executed")

    def __getitem__(self, _key):
        return self._explode()

    def __iter__(self):
        return self._explode()

    def items(self):
        return self._explode()

    def keys(self):
        return self._explode()

    def values(self):
        return self._explode()


class _ExplosiveList(list):
    def __init__(self, value) -> None:
        list.__init__(self, value)
        self.calls = 0

    def _explode(self):
        self.calls += 1
        raise AssertionError("caller list callback executed")

    def __iter__(self):
        return self._explode()

    def __getitem__(self, _key):
        return self._explode()


class _ExplosiveText(str):
    def __new__(cls, value):
        instance = str.__new__(cls, value)
        instance.calls = 0
        return instance

    def _explode(self):
        self.calls += 1
        raise AssertionError("caller text callback executed")

    def __str__(self):
        return self._explode()

    def strip(self, *_args, **_kwargs):
        return self._explode()

    def encode(self, *_args, **_kwargs):
        return self._explode()


class AdmittedFinancialRequestAuthorityTests(unittest.TestCase):
    @staticmethod
    def _bound_case(store: JournalStore):
        fixture = FinancialBindingDispatchTests(methodName="runTest")
        return fixture._bound_case(store)

    def test_issuer_seals_exact_durable_financial_identity(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            admitted, material, request = self._bound_case(store)
            issuer = AdmittedFinancialRequestAuthorityIssuer(store)

            authority = issuer.issue(admitted.admission_id)
            identity = issuer.identity(authority)

            self.assertEqual(identity.admission_id, admitted.admission_id)
            self.assertEqual(identity.binding_id, material.binding_id)
            self.assertEqual(identity.risk_snapshot_id, material.risk_snapshot_id)
            self.assertEqual(identity.risk_decision_id, material.risk_decision_id)
            self.assertEqual(identity.account_cut_id, material.account_cut_id)
            self.assertEqual(
                identity.qualification_identity_digest,
                material.qualification_identity_digest,
            )
            self.assertEqual(
                identity.capability_snapshot_id,
                material.capability_snapshot_id,
            )
            self.assertEqual(identity.provider_scope_digest, material.provider_scope_digest)
            self.assertEqual(identity.request_sha256, material.request_sha256)
            self.assertEqual(
                identity.submission_scope_digest,
                material.submission_scope_digest,
            )

            scope = financial_submission_scope(
                admission_id=admitted.admission_id,
                material=material,
            )
            resolved = issuer.require_exact_request(
                authority,
                request=request,
                submission_scope=scope,
            )
            self.assertEqual(resolved, material)

    def test_public_constructor_and_object_new_forge_are_not_minting_paths(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            admitted, material, request = self._bound_case(store)
            issuer = AdmittedFinancialRequestAuthorityIssuer(store)
            scope = financial_submission_scope(
                admission_id=admitted.admission_id,
                material=material,
            )

            with self.assertRaisesRegex(
                AdmittedFinancialRequestAuthorityError,
                "issued",
            ):
                AdmittedFinancialRequestAuthority()

            forged = object.__new__(AdmittedFinancialRequestAuthority)
            with self.assertRaisesRegex(
                AdmittedFinancialRequestAuthorityError,
                "not issued",
            ):
                issuer.require_exact_request(
                    forged,
                    request=request,
                    submission_scope=scope,
                )

    def test_authority_is_bound_to_one_issuer_instance(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            admitted, material, request = self._bound_case(store)
            first = AdmittedFinancialRequestAuthorityIssuer(store)
            second = AdmittedFinancialRequestAuthorityIssuer(store)
            authority = first.issue(admitted.admission_id)
            scope = financial_submission_scope(
                admission_id=admitted.admission_id,
                material=material,
            )

            with self.assertRaisesRegex(
                AdmittedFinancialRequestAuthorityError,
                "another issuer",
            ):
                second.require_exact_request(
                    authority,
                    request=request,
                    submission_scope=scope,
                )

    def test_request_or_submission_scope_drift_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            admitted, material, request = self._bound_case(store)
            issuer = AdmittedFinancialRequestAuthorityIssuer(store)
            authority = issuer.issue(admitted.admission_id)
            scope = financial_submission_scope(
                admission_id=admitted.admission_id,
                material=material,
            )

            changed_request = {
                **request,
                "body": {**request["body"], "quantity": "2"},
            }
            with self.assertRaisesRegex(
                AdmittedFinancialRequestAuthorityError,
                "request digest",
            ):
                issuer.require_exact_request(
                    authority,
                    request=changed_request,
                    submission_scope=scope,
                )

            changed_scope = {**scope, "capability_snapshot_id": "retargeted"}
            with self.assertRaisesRegex(
                AdmittedFinancialRequestAuthorityError,
                "submission scope",
            ):
                issuer.require_exact_request(
                    authority,
                    request=request,
                    submission_scope=changed_scope,
                )

    def test_copy_pickle_and_mutation_do_not_duplicate_or_retarget_authority(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            admitted, _material, _request = self._bound_case(store)
            issuer = AdmittedFinancialRequestAuthorityIssuer(store)
            authority = issuer.issue(admitted.admission_id)

            with self.assertRaises(AdmittedFinancialRequestAuthorityError):
                copy.copy(authority)
            with self.assertRaises(AdmittedFinancialRequestAuthorityError):
                copy.deepcopy(authority)
            with self.assertRaises(AdmittedFinancialRequestAuthorityError):
                pickle.dumps(authority)
            with self.assertRaises(AttributeError):
                authority.binding_id = "financial-request:sha256:" + "0" * 64

    def test_polymorphic_request_and_scope_are_rejected_without_callbacks(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            admitted, material, request = self._bound_case(store)
            issuer = AdmittedFinancialRequestAuthorityIssuer(store)
            authority = issuer.issue(admitted.admission_id)
            scope = financial_submission_scope(
                admission_id=admitted.admission_id,
                material=material,
            )

            hostile_request = _ExplosiveMapping()
            with self.assertRaisesRegex(TypeError, "request must be an exact dict"):
                issuer.require_exact_request(
                    authority,
                    request=hostile_request,
                    submission_scope=scope,
                )
            self.assertEqual(hostile_request.calls, 0)

            hostile_scope = _ExplosiveMapping()
            with self.assertRaisesRegex(TypeError, "submission_scope must be an exact dict"):
                issuer.require_exact_request(
                    authority,
                    request=request,
                    submission_scope=hostile_scope,
                )
            self.assertEqual(hostile_scope.calls, 0)

    def test_nested_polymorphic_json_values_are_rejected_without_callbacks(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            admitted, material, request = self._bound_case(store)
            issuer = AdmittedFinancialRequestAuthorityIssuer(store)
            authority = issuer.issue(admitted.admission_id)
            scope = financial_submission_scope(
                admission_id=admitted.admission_id,
                material=material,
            )

            hostile_dict = _ExplosiveDict(request["body"])
            with self.assertRaisesRegex(TypeError, "request.body"):
                issuer.require_exact_request(
                    authority,
                    request={**request, "body": hostile_dict},
                    submission_scope=scope,
                )
            self.assertEqual(hostile_dict.calls, 0)

            hostile_list = _ExplosiveList(["one"])
            with self.assertRaisesRegex(TypeError, "request.tags"):
                issuer.require_exact_request(
                    authority,
                    request={**request, "tags": hostile_list},
                    submission_scope=scope,
                )
            self.assertEqual(hostile_list.calls, 0)

            hostile_text = _ExplosiveText("1")
            with self.assertRaisesRegex(TypeError, "request.body.quantity"):
                issuer.require_exact_request(
                    authority,
                    request={
                        **request,
                        "body": {**request["body"], "quantity": hostile_text},
                    },
                    submission_scope=scope,
                )
            self.assertEqual(hostile_text.calls, 0)

            hostile_scope_dict = _ExplosiveDict(
                {"request_sha256": material.request_sha256}
            )
            with self.assertRaisesRegex(TypeError, "submission_scope.extra"):
                issuer.require_exact_request(
                    authority,
                    request=request,
                    submission_scope={**scope, "extra": hostile_scope_dict},
                )
            self.assertEqual(hostile_scope_dict.calls, 0)

    def test_noncanonical_json_scalars_and_keys_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            admitted, material, request = self._bound_case(store)
            issuer = AdmittedFinancialRequestAuthorityIssuer(store)
            authority = issuer.issue(admitted.admission_id)
            scope = financial_submission_scope(
                admission_id=admitted.admission_id,
                material=material,
            )

            with self.assertRaisesRegex(TypeError, "request.body.quantity"):
                issuer.require_exact_request(
                    authority,
                    request={
                        **request,
                        "body": {**request["body"], "quantity": 1.0},
                    },
                    submission_scope=scope,
                )

            with self.assertRaisesRegex(TypeError, "request.body keys"):
                issuer.require_exact_request(
                    authority,
                    request={
                        **request,
                        "body": {**request["body"], 1: "forbidden"},
                    },
                    submission_scope=scope,
                )

    def test_malformed_unicode_fails_as_financial_authority_error_before_digest(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            admitted, material, request = self._bound_case(store)
            issuer = AdmittedFinancialRequestAuthorityIssuer(store)
            authority = issuer.issue(admitted.admission_id)
            scope = financial_submission_scope(
                admission_id=admitted.admission_id,
                material=material,
            )
            malformed = "\ud800"

            with self.assertRaisesRegex(
                AdmittedFinancialRequestAuthorityError,
                "valid canonical UTF-8",
            ):
                issuer.require_exact_request(
                    authority,
                    request={
                        **request,
                        "body": {**request["body"], "quantity": malformed},
                    },
                    submission_scope=scope,
                )

            with self.assertRaisesRegex(
                AdmittedFinancialRequestAuthorityError,
                "valid canonical UTF-8",
            ):
                issuer.require_exact_request(
                    authority,
                    request={
                        **request,
                        "body": {**request["body"], malformed: "forbidden"},
                    },
                    submission_scope=scope,
                )

            with self.assertRaisesRegex(
                AdmittedFinancialRequestAuthorityError,
                "valid canonical UTF-8",
            ):
                issuer.require_exact_request(
                    authority,
                    request=request,
                    submission_scope={**scope, "extra": malformed},
                )

    def test_cyclic_exact_json_is_rejected_before_digest_recursion(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            admitted, material, request = self._bound_case(store)
            issuer = AdmittedFinancialRequestAuthorityIssuer(store)
            authority = issuer.issue(admitted.admission_id)
            scope = financial_submission_scope(
                admission_id=admitted.admission_id,
                material=material,
            )

            cyclic_dict = {}
            cyclic_dict["self"] = cyclic_dict
            with self.assertRaisesRegex(
                AdmittedFinancialRequestAuthorityError,
                "acyclic JSON value",
            ):
                issuer.require_exact_request(
                    authority,
                    request={**request, "extra": cyclic_dict},
                    submission_scope=scope,
                )

            cyclic_list = []
            cyclic_list.append(cyclic_list)
            with self.assertRaisesRegex(
                AdmittedFinancialRequestAuthorityError,
                "acyclic JSON value",
            ):
                issuer.require_exact_request(
                    authority,
                    request=request,
                    submission_scope={**scope, "extra": cyclic_list},
                )

    def test_admission_id_requires_canonical_utf8_before_registry_lookup(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            issuer = AdmittedFinancialRequestAuthorityIssuer(store)

            with self.assertRaisesRegex(
                AdmittedFinancialRequestAuthorityError,
                "valid canonical UTF-8",
            ):
                issuer.issue("\ud800")

    def test_issue_surface_accepts_no_material_or_financial_authority_overrides(self) -> None:
        parameters = inspect.signature(
            AdmittedFinancialRequestAuthorityIssuer.issue
        ).parameters
        self.assertEqual(tuple(parameters), ("self", "admission_id"))
        for forbidden in (
            "material",
            "binding_id",
            "risk_snapshot_id",
            "risk_decision_id",
            "account_cut_id",
            "qualification_identity_digest",
            "capability_snapshot_id",
            "provider_scope_digest",
            "request_sha256",
            "submission_scope_digest",
        ):
            self.assertNotIn(forbidden, parameters)


if __name__ == "__main__":
    unittest.main()
