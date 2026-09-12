import sqlite3
import unittest
from datetime import datetime, timezone
from uuid import UUID, uuid4

from mikrofix_sync_contracts import (
    AttachmentContract,
    BillingCustomerSelectionContract,
    BillingLineContract,
    ContractError,
    IssueServiceInvoiceContract,
    PaymentContract,
    ServicePartContract,
    ServiceTestContract,
    SyncOperationEnvelope,
    TechnicalCompletionContract,
    WorkIntervalContract,
    canonical_json,
    payload_sha256,
    validated_create_service_order_payload,
    validated_create_partner_payload,
    validated_issue_service_invoice_payload,
    validated_issue_direct_sale_invoice_payload,
    validated_technical_completion_payload,
)
from mikrofix_workflows import (
    create_service_order,
    ensure_workflow_schema,
    get_service_order,
    pause_service_work_timer,
    save_service_order_parts,
    start_service_work_timer,
)


def service_order_payload(**changes):
    payload = {
        "customer_name": "TEST Stranka",
        "customer_address": "Testna 1",
        "customer_phone": "040000000",
        "customer_email": "",
        "device_model": "TEST naprava",
        "device_serial": "SYNC-TEST-001",
        "device_imei": "",
        "device_os": "",
        "device_storage": "",
        "device_type": "DRUGO",
        "identity_method": "MANUAL_OTHER",
        "manual_identity_confirmed": True,
        "customer_request": "Test sinhronizacijske osnove",
        "intake_condition": "TEST",
        "accessories": "",
        "quote_eur": "0",
        "logistics_eur": "0",
        "expected_due": "",
        "contract_context": "MFD",
        "immediate_start_requested": False,
        "withdrawal_loss_ack": False,
        "delivery_channel": "PAPER",
        "microdetect_confirmed": False,
        "microdetect_at": "2026-09-11T08:00:00+02:00",
        "operator_code": "TEST",
        "notes": "",
    }
    payload.update(changes)
    return payload


class WorkflowSyncIdentityTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        ensure_workflow_schema(self.connection)

    def tearDown(self):
        self.connection.close()

    def test_new_service_order_has_stable_uuid_and_version(self):
        requested_uuid = str(uuid4())
        order_id, _ = create_service_order(
            self.connection,
            service_order_payload(
                sync_uuid=requested_uuid, origin_device_id="SERVICE-PC-TEST"
            ),
        )
        first = get_service_order(self.connection, order_id)
        ensure_workflow_schema(self.connection)
        second = get_service_order(self.connection, order_id)

        self.assertEqual(first["sync_uuid"], requested_uuid)
        self.assertEqual(second["sync_uuid"], requested_uuid)
        self.assertEqual(first["row_version"], 1)
        self.assertEqual(first["central_version"], 0)
        self.assertEqual(first["origin_device_id"], "SERVICE-PC-TEST")
        self.assertEqual(first["sync_state"], "LOCAL_ONLY")

    def test_additive_migration_backfills_legacy_rows_once(self):
        legacy = sqlite3.connect(":memory:")
        try:
            ensure_workflow_schema(legacy)
            legacy.execute(
                "INSERT INTO service_orders("
                "order_no,created_at,status,customer_name,device_model,device_serial,"
                "customer_request,contract_context,delivery_channel,microdetect_at,"
                "operator_code,sync_uuid"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    "MF-SN-2026-9999", "2026-09-11 08:00:00", "OPEN",
                    "Legacy", "Legacy device", "LEGACY-001", "Repair", "MFD",
                    "PAPER", "2026-09-11 08:00:00", "TEST",
                ),
            )
            legacy.commit()
            ensure_workflow_schema(legacy)
            first = legacy.execute(
                "SELECT sync_uuid FROM service_orders WHERE order_no=?",
                ("MF-SN-2026-9999",),
            ).fetchone()[0]
            ensure_workflow_schema(legacy)
            second = legacy.execute(
                "SELECT sync_uuid FROM service_orders WHERE order_no=?",
                ("MF-SN-2026-9999",),
            ).fetchone()[0]
            self.assertEqual(str(UUID(first)), first)
            self.assertEqual(second, first)
        finally:
            legacy.close()

    def test_intervals_and_part_rows_receive_uuid_identity(self):
        order_id, _ = create_service_order(self.connection, service_order_payload())
        start_service_work_timer(
            self.connection,
            order_id,
            started_at=datetime(2026, 9, 11, 8, 0, 0),
            origin_device_id="SERVICE-PC-TEST",
        )
        save_service_order_parts(
            self.connection,
            order_id,
            [("Test part", "PART-1", "BAR-1", 1, "LOT-1", 12.5)],
        )
        interval = self.connection.execute(
            "SELECT interval_uuid,origin_device_id,sync_state "
            "FROM service_work_sessions WHERE service_order_id=?",
            (order_id,),
        ).fetchone()
        part = self.connection.execute(
            "SELECT line_uuid,sync_state FROM service_order_parts "
            "WHERE service_order_id=?",
            (order_id,),
        ).fetchone()
        self.assertEqual(str(UUID(interval[0])), interval[0])
        self.assertEqual(interval[1:], ("SERVICE-PC-TEST", "LOCAL_ONLY"))
        self.assertEqual(str(UUID(part[0])), part[0])
        self.assertEqual(part[1], "LOCAL_ONLY")

    def test_row_version_advances_once_per_local_timer_command(self):
        order_id, _ = create_service_order(self.connection, service_order_payload())
        self.assertEqual(get_service_order(self.connection, order_id)["row_version"], 1)
        start_service_work_timer(
            self.connection,
            order_id,
            started_at=datetime(2026, 9, 11, 8, 0, 0),
        )
        self.assertEqual(get_service_order(self.connection, order_id)["row_version"], 2)
        pause_service_work_timer(
            self.connection,
            order_id,
            ended_at=datetime(2026, 9, 11, 8, 10, 0),
        )
        self.assertEqual(get_service_order(self.connection, order_id)["row_version"], 3)
        pause_service_work_timer(
            self.connection,
            order_id,
            ended_at=datetime(2026, 9, 11, 8, 20, 0),
        )
        self.assertEqual(
            get_service_order(self.connection, order_id)["row_version"], 3,
            "a repeated pause without a running interval is not a new mutation",
        )


class SyncContractTests(unittest.TestCase):
    def test_canonical_payload_hash_ignores_mapping_order(self):
        left = {"č": "vrednost", "a": {"z": 2, "b": 1}}
        right = {"a": {"b": 1, "z": 2}, "č": "vrednost"}
        self.assertEqual(canonical_json(left), canonical_json(right))
        self.assertEqual(payload_sha256(left), payload_sha256(right))

    def test_create_contract_checks_business_minimum(self):
        payload = service_order_payload(sync_uuid=str(uuid4()))
        validated = validated_create_service_order_payload(payload)
        self.assertEqual(validated["quote_eur"], "0")
        invalid = dict(payload, customer_phone="", customer_email="")
        with self.assertRaisesRegex(ContractError, "customer_phone"):
            validated_create_service_order_payload(invalid)

    def test_envelope_requires_offset_time_and_has_stable_hash(self):
        payload = {"service_order_id": str(uuid4()), "state": "OPEN"}
        envelope = SyncOperationEnvelope(
            operation_id=str(uuid4()),
            operation_type="CreateServiceOrder",
            aggregate_type="SERVICE_ORDER",
            aggregate_id=payload["service_order_id"],
            expected_version=0,
            actor_id="serviser",
            device_id="SERVICE-PC-TEST",
            device_sequence=1,
            occurred_at="2026-09-11T08:30:00+02:00",
            payload=payload,
        )
        self.assertEqual(envelope.payload_hash, payload_sha256(payload))
        self.assertEqual(envelope.as_dict()["contract_version"], 1)
        with self.assertRaisesRegex(ContractError, "UTC offset"):
            SyncOperationEnvelope(
                operation_id=str(uuid4()), operation_type="TEST",
                aggregate_type="SERVICE_ORDER", aggregate_id=str(uuid4()),
                expected_version=0, actor_id="a", device_id="d",
                device_sequence=1, occurred_at="2026-09-11T08:30:00", payload={},
            )

    def test_technical_completion_contract_is_transport_neutral(self):
        interval = WorkIntervalContract(
            interval_id=str(uuid4()), technician_id="serviser",
            started_at="2026-09-11T08:00:00+02:00",
            ended_at="2026-09-11T08:30:00+02:00", elapsed_seconds=1800,
        )
        part = ServicePartContract(
            line_id=str(uuid4()), article_id="ARTICLE-1", description="Test part",
            quantity="1.00", unit_price="12.50", barcode="BAR-1",
            lot_serial="LOT-1",
        )
        test = ServiceTestContract(stage="PO", name="Polnjenje", status="OK")
        attachment = AttachmentContract(
            attachment_id=str(uuid4()), sha256="a" * 64, size_bytes=123,
            media_type="image/jpeg", stage="AFTER",
        )
        billing_line = BillingLineContract(
            line_id=str(uuid4()), line_kind="T1", description="Delo",
            quantity="1", unit_price="30", billable_minutes=30,
            tariff_version="T1-v1",
        )
        completion = TechnicalCompletionContract(
            service_order_id=str(uuid4()), expected_version=1,
            completed_at=datetime.now(timezone.utc).isoformat(),
            billing_disposition="INVOICE_REQUIRED", work_summary="Repair",
            findings="Test", billing_lines=(billing_line,),
            intervals=(interval,), parts=(part,),
            tests=(test,), attachments=(attachment,),
        )
        self.assertEqual(completion.payload()["parts"][0]["quantity"], "1")
        validated = validated_technical_completion_payload(completion.payload())
        self.assertEqual(validated["tests"][0]["status"], "OK")

    def test_technical_completion_always_requires_invoice(self):
        with self.assertRaisesRegex(ContractError, "billing_disposition"):
            TechnicalCompletionContract(
                service_order_id=str(uuid4()), expected_version=1,
                completed_at=datetime.now(timezone.utc).isoformat(),
                billing_disposition="WARRANTY_NO_INVOICE",
                work_summary="Warranty repair", findings="Test",
            )

    def test_contract_rejects_invalid_part_quantity(self):
        with self.assertRaisesRegex(ContractError, "greater than zero"):
            ServicePartContract(
                line_id=str(uuid4()), article_id="ARTICLE-1", description="Part",
                quantity="0", unit_price="1", barcode="BAR-1",
            )

    def test_invoice_issue_contract_contains_choices_but_no_invoice_content(self):
        draft_id = str(uuid4())
        issue = IssueServiceInvoiceContract(
            draft_id=draft_id,
            expected_version=1,
            premise_id="mfd",
            electronic_device_id="pis",
            billing_customer=BillingCustomerSelectionContract(
                source="SERVICE_CUSTOMER", confirmed=True,
            ),
            payments=(PaymentContract("BANK_TRANSFER", "55", False),),
        )
        self.assertEqual(issue.payload()["premise_id"], "MFD")
        with self.assertRaisesRegex(ContractError, "unsupported fields: discount"):
            validated_issue_service_invoice_payload({
                **issue.payload(),
                "discount": "10",
            })

    def test_direct_sale_allows_walk_in_but_server_owns_price_and_lots(self):
        payload = validated_issue_direct_sale_invoice_payload({
            "sale_id": str(uuid4()), "expected_version": 0,
            "premise_id": "MFD", "electronic_device_id": "B1",
            "billing_customer": {
                "source": "WALK_IN", "confirmed": True,
                "partner_id": "", "expected_version": 0,
            },
            "payments": [{
                "method": "CASH", "amount": "25", "confirmed": True,
            }],
            "lines": [{
                "line_id": str(uuid4()), "barcode": "SUP-1", "quantity": "2",
                "requested_discount_percent": "100", "lot_serial": "",
            }],
        })
        self.assertEqual(payload["billing_customer"]["source"], "WALK_IN")
        self.assertEqual(payload["lines"][0]["quantity"], "2")
        self.assertNotIn("unit_price", payload["lines"][0])
        with self.assertRaisesRegex(ContractError, "payload is invalid"):
            validated_issue_direct_sale_invoice_payload({
                **payload,
                "lines": [{**payload["lines"][0], "unit_price": "0.01"}],
            })

    def test_split_bank_share_must_already_be_confirmed(self):
        with self.assertRaisesRegex(ContractError, "split payment"):
            IssueServiceInvoiceContract(
                draft_id=str(uuid4()), expected_version=1,
                premise_id="MFD", electronic_device_id="PIS",
                billing_customer=BillingCustomerSelectionContract(
                    source="SERVICE_CUSTOMER", confirmed=True,
                ),
                payments=(
                    PaymentContract("CASH", "10", True),
                    PaymentContract("BANK_TRANSFER", "20", False),
                ),
            )

    def test_central_partner_selection_requires_identity_and_version(self):
        partner_id = str(uuid4())
        selected = BillingCustomerSelectionContract(
            source="CENTRAL_PARTNER", confirmed=True,
            partner_id=partner_id, expected_version=3,
        )
        self.assertEqual(selected.partner_id, partner_id)
        with self.assertRaisesRegex(ContractError, "explicitly confirmed"):
            BillingCustomerSelectionContract(
                source="CENTRAL_PARTNER", confirmed=False,
                partner_id=partner_id, expected_version=3,
            )

    def test_new_partner_requires_review_and_ajpes_identity(self):
        partner_id = str(uuid4())
        payload = validated_create_partner_payload({
            "partner_id": partner_id,
            "verification_source": "AJPES_REVIEW",
            "review_confirmed": True,
            "name": "Primer d.o.o.",
            "address": "Testna 1",
            "post_city": "1000 Ljubljana",
            "country": "Slovenija",
            "tax_number": "SI12345678",
            "registration_number": "1234567000",
            "phone": "",
            "email": "racuni@example.test",
            "source_reference": "AJPES:12345678",
        })
        self.assertEqual(payload["tax_number"], "12345678")
        with self.assertRaisesRegex(ContractError, "review confirmation"):
            validated_create_partner_payload({**payload, "review_confirmed": False})


if __name__ == "__main__":
    unittest.main()
