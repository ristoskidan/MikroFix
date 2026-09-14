import ast
import unittest
from pathlib import Path


SOURCE_PATH = Path(__file__).with_name("mikrofix.py")


class ServiceReturnUiPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE_PATH.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source, filename=str(SOURCE_PATH))
        cls.functions = {
            node.name: node for node in cls.tree.body
            if isinstance(node, ast.FunctionDef)
        }

    def calls_in(self, function_name):
        calls = []
        for node in ast.walk(self.functions[function_name]):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    calls.append(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    calls.append(node.func.attr)
        return calls

    def test_claim_view_uses_synced_exact_lot_and_local_outbox(self):
        calls = self.calls_in("record_removed_part_for_selected_service")
        self.assertIn("service_return_snapshot_complete", calls)
        self.assertIn("service_returnable_parts", calls)
        self.assertIn("enqueue_removed_service_part", calls)
        self.assertIn("request_service_sync", calls)
        self.assertNotIn("consume_service_parts", calls)

    def test_warning_states_no_stock_or_document_side_effect(self):
        function = ast.get_source_segment(
            self.source,
            self.functions["record_removed_part_for_selected_service"],
        )
        self.assertIn("gre v karanteno", function)
        self.assertIn("zaloga se ne poveča", function)
        self.assertIn("izvirni servis pa ostaneta nespremenjena", function)
        self.assertIn("NE bo dodan v zalogo", function)

    def test_claim_tab_exposes_explicit_command(self):
        self.assertIn('text="EVIDENTIRAJ ODSTRANJEN DEL"', self.source)
        self.assertIn(
            "command=record_removed_part_for_selected_service", self.source
        )


if __name__ == "__main__":
    unittest.main()
