import unittest

from tools.verify import verification_commands


class VerifyOrchestrationContractTests(unittest.TestCase):
    def test_dotnet_contract_harness_receives_every_v6_corpus(self):
        commands = verification_commands(platform="linux")
        contract_commands = [
            command
            for command in commands
            if "tests/Contracts.DotNet/Contracts.DotNet.csproj" in command
        ]
        self.assertEqual(len(contract_commands), 1)
        self.assertEqual(
            contract_commands[0][-3:],
            (
                "contracts/fixtures/common-scalars.corpus.json",
                "contracts/fixtures/dataset-manifest.semantic.corpus.json",
                "contracts/fixtures/contract-shapes.corpus.json",
            ),
        )


if __name__ == "__main__":
    unittest.main()
