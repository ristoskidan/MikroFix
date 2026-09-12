import ast
import unittest
from pathlib import Path


SOURCE_PATH = Path(__file__).with_name("mikrofix.py")
STOCK_SOURCE_PATH = Path(__file__).with_name("mikrofix_service_stock.py")


class ServiceCompletionPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE_PATH.read_text(encoding="utf-8")
        cls.stock_source = STOCK_SOURCE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(cls.source, filename=str(SOURCE_PATH))
        cls.function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_save_service"
        )

    def calls(self):
        result = []
        for node in ast.walk(self.function):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                result.append(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                result.append(node.func.attr)
        return result

    def strings(self):
        return {
            node.value for node in ast.walk(self.function)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }

    def test_completion_and_local_outbox_share_the_business_transaction(self):
        calls = self.calls()
        self.assertIn("finish_service_work_timer", calls)
        self.assertIn("link_service_result", calls)
        self.assertIn("prepare_operation", calls)
        self.assertIn("complete_operation", calls)
        self.assertIn("commit", calls)

    def test_service_consumes_stock_and_marks_invoice_draft_ready(self):
        strings = self.strings()
        self.assertIn("consume_service_parts", self.calls())
        self.assertIn("SERVICE_CONSUMPTION", self.stock_source)
        self.assertIn("SERVICE_USE_OUT", self.stock_source)
        self.assertIn("READY", strings)
        self.assertNotIn("SERVICE_RESERVATION", self.stock_source)
        self.assertNotIn("RESERVE_SERVICE", self.stock_source)

    def test_completion_never_opens_cashier(self):
        calls = self.calls()
        self.assertNotIn("prepare_invoice_for_service", calls)
        self.assertNotIn("generate_pdf", calls)
        self.assertNotIn("askyesno", calls)
        self.assertIn("showinfo", calls)

    def test_repeat_click_checks_existing_service_result(self):
        source = ast.get_source_segment(self.source, self.function)
        self.assertIn('existing_order.get("service_id")', source)
        self.assertIn("Ponovni klik ni ustvaril", source)


if __name__ == "__main__":
    unittest.main()
