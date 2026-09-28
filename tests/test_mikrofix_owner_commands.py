from __future__ import annotations

import unittest

from tools.mikrofix_owner_commands import (
    OwnerCommandError, help_text, parse_owner_command,
)


class OwnerCommandParserTests(unittest.TestCase):
    def test_ordinary_conversation_is_not_a_command(self):
        self.assertIsNone(parse_owner_command("Preveri trenutno implementacijo."))

    def test_canonical_commands_and_legacy_aliases_are_normalized(self):
        for source, expected in (
            ("/prepare-plan", "/prepare-plan"),
            (" /prepareplan ", "/prepare-plan"),
            (" /plandispatch ", "/plan-dispatch"),
            (" /dispatch-plan ", "/plan-dispatch"),
            (" /dispatchplan ", "/plan-dispatch"),
            (" /rejectplan popravi kriterije ", "/reject-plan"),
            (" /revise-plan ", "/reject-plan"),
            ("/preparereview", "/prepare-review"),
            ("/codexrejected konkretna ugotovitev", "/codex-rejected"),
            ("/codexapproved abc1234", "/codex-approved"),
            ("/replace-goal Popravi samo parser", "/replace-goal"),
            ("/research [OWNER_REQUEST] x", "/research"),
        ):
            with self.subTest(source=source):
                self.assertEqual(expected, parse_owner_command(source).name)

    def test_unknown_command_fails_instead_of_reaching_the_model(self):
        with self.assertRaisesRegex(OwnerCommandError, "Neznan ukaz"):
            parse_owner_command("/run-anything now")

    def test_argument_contracts_fail_closed(self):
        with self.assertRaisesRegex(OwnerCommandError, "ne sprejme"):
            parse_owner_command("/prepare-plan extra")
        with self.assertRaisesRegex(OwnerCommandError, "razlog zavrnitve"):
            parse_owner_command("/codex-rejected")
        with self.assertRaisesRegex(OwnerCommandError, "commit SHA"):
            parse_owner_command("/codex-approved")
        with self.assertRaisesRegex(OwnerCommandError, "nov potrjeni cilj"):
            parse_owner_command("/replace-goal")
        with self.assertRaisesRegex(OwnerCommandError, "worker_model"):
            parse_owner_command("/research")

    def test_help_describes_only_implemented_semantics(self):
        text = help_text()
        self.assertIn("/status", text)
        self.assertIn("/codex-opinion", text)
        self.assertIn("/replace-goal", text)
        self.assertIn("/plan-dispatch", text)
        self.assertIn("/reject-plan", text)
        self.assertIn("preveri poti/blobe", text)
        self.assertNotIn("/applied", text)


if __name__ == "__main__":
    unittest.main()
