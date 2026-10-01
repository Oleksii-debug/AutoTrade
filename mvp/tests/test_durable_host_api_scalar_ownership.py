import unittest

from mvp.autotrade_mvp import _generated_common_scalars
from mvp.autotrade_mvp import durable_host_api


class DurableHostApiScalarOwnershipTests(unittest.TestCase):
    def test_host_uses_package_local_generated_common_scalar_authority(self):
        self.assertIs(
            durable_host_api.is_valid_common_scalar,
            _generated_common_scalars.is_valid_common_scalar,
        )
        self.assertEqual(
            durable_host_api.is_valid_common_scalar.__module__,
            "mvp.autotrade_mvp._generated_common_scalars",
        )


if __name__ == "__main__":
    unittest.main()
