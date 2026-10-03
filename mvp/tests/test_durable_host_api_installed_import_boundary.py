from __future__ import annotations

import ast
from pathlib import Path
import unittest

import mvp.autotrade_mvp.durable_host_api as durable_host_api


class DurableHostApiInstalledImportBoundaryTests(unittest.TestCase):
    def test_common_scalar_validator_is_package_local_not_checkout_contracts(self):
        source_path = Path(durable_host_api.__file__).resolve()
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))

        imports = {
            (node.level, node.module)
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        self.assertIn((1, "_generated_common_scalars"), imports)
        self.assertFalse(
            any(
                module is not None and module.startswith("contracts")
                for _level, module in imports
            ),
            "installed durable host API must not depend on repository contracts bindings",
        )


if __name__ == "__main__":
    unittest.main()
