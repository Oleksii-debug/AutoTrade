import unittest

from autotrade_foundation import windows_namespace


class WindowsNamespaceWriteThroughContractTests(unittest.TestCase):
    def test_publication_option_adds_only_nt_write_through(self):
        ordinary = windows_namespace._regular_file_create_options(
            write_through=False,
        )
        durable = windows_namespace._regular_file_create_options(
            write_through=True,
        )
        self.assertEqual(
            durable,
            ordinary | windows_namespace._NT_FILE_WRITE_THROUGH,
        )
        self.assertEqual(
            ordinary & windows_namespace._NT_FILE_WRITE_THROUGH,
            0,
        )
        self.assertEqual(
            durable & windows_namespace._NT_FILE_WRITE_THROUGH,
            windows_namespace._NT_FILE_WRITE_THROUGH,
        )

    def test_write_through_selector_rejects_non_boolean_values(self):
        for value in (0, 1, None, "true"):
            with self.subTest(value=value), self.assertRaises(TypeError):
                windows_namespace._regular_file_create_options(
                    write_through=value,
                )


if __name__ == "__main__":
    unittest.main()
