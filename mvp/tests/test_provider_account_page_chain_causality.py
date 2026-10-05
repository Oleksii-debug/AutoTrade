from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.provider_account_page_chain import (
    ProviderAccountPageChainError,
    issue_provider_account_page_chain,
)
from mvp.tests.test_provider_account_page_chain import (
    NOW,
    SURFACE,
    ProviderAccountPageChainTests,
)


class ProviderAccountPageChainCausalityTests(unittest.TestCase):
    def helper(self) -> ProviderAccountPageChainTests:
        helper = ProviderAccountPageChainTests(
            methodName="test_single_explicit_terminal_page_issues_sealed_chain"
        )
        self.addCleanup(helper.doCleanups)
        return helper

    def test_guessed_next_cursor_observed_before_root_cannot_be_stitched(self):
        helper = self.helper()
        with TemporaryDirectory() as directory:
            fixture = helper._fixture(directory)
            root_binding = helper._binding(fixture)
            guessed_binding = helper._binding(fixture, cursor="cursor-2")

            guessed_response = helper._direct_response(
                fixture,
                guessed_binding,
                body=b'{"retCode":0,"result":{"list":[],"nextPageCursor":""}}',
                marker="guessed-before-root",
            )
            root_response = helper._direct_response(
                fixture,
                root_binding,
                body=b'{"retCode":0,"result":{"list":[],"nextPageCursor":"cursor-2"}}',
                marker="root-after-guessed",
            )
            origin_set = helper._origin_set(
                fixture,
                (root_response, guessed_response),
            )
            observations = (
                helper._observation(root_response, root_binding),
                helper._observation(guessed_response, guessed_binding),
            )

            with self.assertRaisesRegex(
                ProviderAccountPageChainError,
                "durable causal order",
            ):
                issue_provider_account_page_chain(
                    absence_semantics=fixture[7],
                    origin_set=origin_set,
                    observations=observations,
                    qualification_registry=fixture[2],
                    surface=SURFACE,
                    at=NOW,
                )


if __name__ == "__main__":
    unittest.main()
