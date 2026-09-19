"""PostgreSQL integration tests for the first central API slice.

Run explicitly with MIKROFIX_RUN_PG_TESTS=1 after applying the test migration.
The ordinary SQLite suite therefore never mutates a developer database.
"""

from __future__ import annotations

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4


RUN_POSTGRES_TESTS = os.environ.get("MIKROFIX_RUN_PG_TESTS") == "1"


@unittest.skipUnless(RUN_POSTGRES_TESTS, "PostgreSQL integration tests are opt-in")
class CentralApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        from mikrofix_server.app import create_app
        from mikrofix_server.config import ServerSettings
        from mikrofix_server.database import create_database_engine

        cls.token = "test-token-with-at-least-24-characters"
        cls.engine = create_database_engine(
            os.environ.get(
                "MIKROFIX_TEST_DATABASE_URL",
                "postgresql+psycopg:///mikrofix_test",
            )
        )
        settings = ServerSettings(
            database_url=str(cls.engine.url),
            sync_token=cls.token,
            environment="test",
        )
        cls.client = TestClient(create_app(settings, engine=cls.engine))

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.engine.dispose()

    def setUp(self):
        self.clean_server_tables()

    def tearDown(self):
        self.clean_server_tables()

    def clean_server_tables(self):
        from sqlalchemy import delete, update

        from mikrofix_server.models import (
            accepted_operations,
            cash_journal_entries,
            cash_session_number_sequences,
            cash_sessions,
            central_audit_events,
            central_sessions,
            central_user_permissions,
            central_user_permission_history,
            central_users,
            change_feed,
            direct_sale_discount_audit,
            direct_sale_stock_allocations,
            fiscal_outbox,
            full_inventory_sessions,
            inventory_control,
            inventory_items,
            invoice_drafts,
            invoice_number_sequences,
            invoices,
            partners,
            physical_stock_returns,
            purchase_receipt_lines,
            purchase_receipt_revisions,
            purchase_receipts,
            receipt_attachments,
            receipt_document_types,
            receipt_number_sequences,
            inventory_identities,
            inventory_lots,
            inventory_price_history,
            inventory_service_readiness,
            service_orders,
            service_part_consumptions,
            service_stock_allocations,
            technical_completions,
            workstations,
        )

        with self.engine.begin() as connection:
            connection.execute(
                update(inventory_control)
                .where(inventory_control.c.control_id == 1)
                .values(mode="NORMAL", current_session_uuid=None)
            )
            connection.execute(delete(central_audit_events))
            connection.execute(delete(central_sessions))
            connection.execute(delete(inventory_service_readiness))
            connection.execute(delete(physical_stock_returns))
            connection.execute(delete(service_stock_allocations))
            connection.execute(delete(receipt_attachments))
            connection.execute(delete(direct_sale_discount_audit))
            connection.execute(delete(direct_sale_stock_allocations))
            connection.execute(delete(inventory_price_history))
            connection.execute(delete(inventory_lots))
            connection.execute(delete(inventory_identities))
            connection.execute(delete(purchase_receipt_lines))
            connection.execute(delete(purchase_receipt_revisions))
            connection.execute(delete(purchase_receipts))
            connection.execute(delete(receipt_number_sequences))
            connection.execute(delete(receipt_document_types))
            connection.execute(delete(fiscal_outbox))
            connection.execute(delete(cash_journal_entries))
            connection.execute(delete(invoices))
            connection.execute(delete(cash_sessions))
            connection.execute(delete(cash_session_number_sequences))
            connection.execute(delete(invoice_number_sequences))
            connection.execute(delete(invoice_drafts))
            connection.execute(delete(service_part_consumptions))
            connection.execute(delete(technical_completions))
            connection.execute(delete(full_inventory_sessions))
            connection.execute(delete(accepted_operations))
            connection.execute(delete(change_feed))
            connection.execute(delete(service_orders))
            connection.execute(delete(inventory_items))
            connection.execute(delete(partners))
            connection.execute(delete(workstations))
            connection.execute(delete(central_user_permissions))
            connection.execute(delete(central_user_permission_history))
            connection.execute(delete(central_users))

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def operation(self, *, sequence=1):
        aggregate_id = str(uuid4())
        return {
            "operation_id": str(uuid4()),
            "operation_type": "CreateServiceOrder",
            "aggregate_type": "SERVICE_ORDER",
            "aggregate_id": aggregate_id,
            "expected_version": 0,
            "actor_id": "serviser-test",
            "device_id": "SERVICE-PC-TEST",
            "device_sequence": sequence,
            "occurred_at": "2026-09-11T08:00:00+02:00",
            "payload": {
                "sync_uuid": aggregate_id,
                "customer_name": "Testna stranka",
                "customer_phone": "040000000",
                "customer_email": "",
                "device_model": "Testna naprava",
                "device_serial": f"TEST-{aggregate_id[:8]}",
                "customer_request": "Diagnostika",
                "identity_method": "MANUAL_OTHER",
                "manual_identity_confirmed": True,
                "contract_context": "MFD",
                "delivery_channel": "PAPER",
                "quote_eur": "0",
                "logistics_eur": "0",
            },
            "contract_version": 1,
        }

    def full_inventory_operation(
        self, session_id, *, operation_type, sequence, expected_version, payload,
    ):
        return {
            "operation_id": str(uuid4()),
            "operation_type": operation_type,
            "aggregate_type": "FULL_INVENTORY",
            "aggregate_id": str(session_id),
            "expected_version": expected_version,
            "actor_id": "office-test",
            "device_id": "OFFICE-PC-TEST",
            "device_sequence": sequence,
            "occurred_at": "2026-09-19T08:00:00+02:00",
            "payload": payload,
            "contract_version": 1,
        }

    def completion_operation(
        self, order_request, *, sequence=2, barcode="PART-1", article_id=None,
    ):
        return {
            "operation_id": str(uuid4()),
            "operation_type": "CompleteServiceTechnically",
            "aggregate_type": "SERVICE_ORDER",
            "aggregate_id": order_request["aggregate_id"],
            "expected_version": 1,
            "actor_id": "serviser-test",
            "device_id": "SERVICE-PC-TEST",
            "device_sequence": sequence,
            "occurred_at": "2026-09-12T09:00:00+02:00",
            "payload": {
                "service_order_id": order_request["aggregate_id"],
                "expected_version": 1,
                "completed_at": "2026-09-12T09:00:00+02:00",
                "billing_disposition": "INVOICE_REQUIRED",
                "work_summary": "Menjava testnega dela",
                "findings": "Okvarjen testni del",
                "billing_lines": [{
                    "line_id": str(uuid4()),
                    "line_kind": "T1",
                    "description": "Delo",
                    "quantity": "1",
                    "unit_price": "30",
                    "billable_minutes": 60,
                    "tariff_version": "T1-v1",
                }],
                "intervals": [{
                    "interval_id": str(uuid4()),
                    "technician_id": "serviser-test",
                    "started_at": "2026-09-12T08:00:00+02:00",
                    "ended_at": "2026-09-12T09:00:00+02:00",
                    "elapsed_seconds": 3600,
                }],
                "parts": [{
                    "line_id": str(uuid4()),
                    "article_id": str(article_id or uuid4()),
                    "description": "Testni del",
                    "quantity": "1",
                    "unit_price": "25",
                    "barcode": barcode,
                    "lot_serial": "LOT-1",
                }],
                "tests": [{"stage": "PO", "name": "Polnjenje", "status": "OK"}],
                "attachments": [],
            },
            "contract_version": 1,
        }

    def issue_operation(
        self, draft_id, *, sequence, method="BANK_TRANSFER", confirmed=False,
        actor="blagajnik-a", device="POS-PC-A", settlement_mode="STANDARD",
    ):
        return {
            "operation_id": str(uuid4()),
            "operation_type": "IssueServiceInvoice",
            "aggregate_type": "INVOICE_DRAFT",
            "aggregate_id": draft_id,
            "expected_version": 1,
            "actor_id": actor,
            "device_id": device,
            "device_sequence": sequence,
            "occurred_at": "2026-09-12T10:00:00+02:00",
            "payload": {
                "draft_id": draft_id,
                "expected_version": 1,
                "premise_id": "MFD",
                "electronic_device_id": "PIS",
                "billing_customer": {
                    "source": "SERVICE_CUSTOMER",
                    "confirmed": True,
                },
                "payments": [{
                    "method": method,
                    "amount": "55",
                    "confirmed": confirmed,
                }] if settlement_mode == "STANDARD" else [],
                "settlement_mode": settlement_mode,
            },
            "contract_version": 1,
        }

    def settle_bank_invoice_operation(self, invoice_id, *, sequence, method):
        return {
            "operation_id": str(uuid4()),
            "operation_type": "SettleUnpaidBankInvoice",
            "aggregate_type": "INVOICE", "aggregate_id": str(invoice_id),
            "expected_version": 1, "actor_id": "blagajnik-a",
            "device_id": "POS-PC-A", "device_sequence": sequence,
            "occurred_at": "2026-09-13T12:00:00+02:00",
            "payload": {
                "invoice_id": str(invoice_id), "expected_version": 1,
                "method": method,
            },
            "contract_version": 1,
        }

    def storno_invoice_operation(
        self, invoice_id, *, sequence, expected_version=1,
        reason="Napačno izdan račun",
    ):
        return {
            "operation_id": str(uuid4()), "operation_type": "StornoInvoice",
            "aggregate_type": "INVOICE", "aggregate_id": str(invoice_id),
            "expected_version": expected_version, "actor_id": "blagajnik-a",
            "device_id": "POS-PC-A", "device_sequence": sequence,
            "occurred_at": "2026-09-14T12:00:00+02:00",
            "payload": {
                "invoice_id": str(invoice_id),
                "expected_version": expected_version,
                "reason": reason, "confirmed": True,
            },
            "contract_version": 1,
        }

    def receive_stock_return_operation(
        self, return_id, invoice_id, line_id, lot_id, *, sequence,
        operation_type="ReceiveReturnedSaleItem", quantity="1",
    ):
        return {
            "operation_id": str(uuid4()), "operation_type": operation_type,
            "aggregate_type": "STOCK_RETURN", "aggregate_id": str(return_id),
            "expected_version": 0, "actor_id": "blagajnik-a",
            "device_id": "POS-PC-A", "device_sequence": sequence,
            "occurred_at": "2026-09-14T13:00:00+02:00",
            "payload": {
                "return_id": str(return_id), "invoice_id": str(invoice_id),
                "invoice_line_id": str(line_id), "lot_id": str(lot_id),
                "quantity": quantity, "condition_note": "Fizično prevzeto",
                "confirmed": True,
            },
            "contract_version": 1,
        }

    def resolve_stock_return_operation(
        self, return_id, *, sequence, decision="RETURN_TO_STOCK",
    ):
        return {
            "operation_id": str(uuid4()),
            "operation_type": "ResolvePhysicalStockReturn",
            "aggregate_type": "STOCK_RETURN", "aggregate_id": str(return_id),
            "expected_version": 1, "actor_id": "office-a",
            "device_id": "OFFICE-PC-A", "device_sequence": sequence,
            "occurred_at": "2026-09-14T14:00:00+02:00",
            "payload": {
                "return_id": str(return_id), "expected_version": 1,
                "decision": decision, "reason": "Pregledano v Office",
                "confirmed": True,
            },
            "contract_version": 1,
        }

    def direct_sale_operation(
        self, sale_id, *, sequence, barcode="SUP-BARCODE-1", quantity="1",
        discount="0", amount="15.00", method="BANK_TRANSFER", confirmed=False,
        lot_serial="",
    ):
        return {
            "operation_id": str(uuid4()),
            "operation_type": "IssueDirectSaleInvoice",
            "aggregate_type": "DIRECT_SALE",
            "aggregate_id": str(sale_id),
            "expected_version": 0,
            "actor_id": "blagajnik-a",
            "device_id": "POS-PC-A",
            "device_sequence": sequence,
            "occurred_at": "2026-09-12T12:00:00+02:00",
            "payload": {
                "sale_id": str(sale_id), "expected_version": 0,
                "premise_id": "MFD", "electronic_device_id": "PIS",
                "billing_customer": {
                    "source": "WALK_IN", "confirmed": True,
                },
                "payments": [{
                    "method": method, "amount": amount, "confirmed": confirmed,
                }],
                "lines": [{
                    "line_id": str(uuid4()), "barcode": barcode,
                    "quantity": quantity,
                    "requested_discount_percent": discount,
                    "lot_serial": lot_serial,
                }],
            },
            "contract_version": 1,
        }

    def open_cash_operation(self, *, sequence, session_id=None, opening="100"):
        session_id = str(session_id or uuid4())
        return {
            "operation_id": str(uuid4()), "operation_type": "OpenCashSession",
            "aggregate_type": "CASH_SESSION", "aggregate_id": session_id,
            "expected_version": 0, "actor_id": "blagajnik-a",
            "device_id": "POS-PC-A", "device_sequence": sequence,
            "occurred_at": "2026-09-12T07:00:00+02:00",
            "payload": {
                "session_id": session_id, "premise_id": "MFD",
                "electronic_device_id": "PIS", "opening_cash": opening,
            },
            "contract_version": 1,
        }

    def close_cash_operation(self, session_id, *, sequence, counted, note=""):
        return {
            "operation_id": str(uuid4()), "operation_type": "CloseCashSession",
            "aggregate_type": "CASH_SESSION", "aggregate_id": str(session_id),
            "expected_version": 1, "actor_id": "blagajnik-a",
            "device_id": "POS-PC-A", "device_sequence": sequence,
            "occurred_at": "2026-09-12T18:00:00+02:00",
            "payload": {
                "session_id": str(session_id), "expected_version": 1,
                "counted_cash": counted, "note": note,
            },
            "contract_version": 1,
        }

    def manual_cash_operation(
        self, session_id, *, sequence, direction, amount,
        reference="BL-TEST", description="Ročni dogodek",
    ):
        entry_id = str(uuid4())
        return {
            "operation_id": str(uuid4()),
            "operation_type": "RecordManualCashEntry",
            "aggregate_type": "CASH_ENTRY", "aggregate_id": entry_id,
            "expected_version": 0, "actor_id": "blagajnik-a",
            "device_id": "POS-PC-A", "device_sequence": sequence,
            "occurred_at": "2026-09-12T12:00:00+02:00",
            "payload": {
                "entry_id": entry_id, "session_id": str(session_id),
                "direction": direction, "amount": amount,
                "reference_no": reference, "description": description,
            },
            "contract_version": 1,
        }

    def create_partner_operation(self, *, sequence, tax_number="12345678"):
        partner_id = str(uuid4())
        return {
            "operation_id": str(uuid4()),
            "operation_type": "CreatePartner",
            "aggregate_type": "PARTNER",
            "aggregate_id": partner_id,
            "expected_version": 0,
            "actor_id": "blagajnik-a",
            "device_id": "POS-PC-A",
            "device_sequence": sequence,
            "occurred_at": "2026-09-12T10:00:00+02:00",
            "payload": {
                "partner_id": partner_id,
                "verification_source": "MANUAL_REVIEW",
                "review_confirmed": True,
                "name": "Novi kupec d.o.o.",
                "address": "Nova cesta 1",
                "post_city": "1000 Ljubljana",
                "country": "Slovenija",
                "tax_number": tax_number,
                "registration_number": "",
                "phone": "",
                "email": "racuni@example.test",
                "source_reference": "F9",
            },
            "contract_version": 1,
        }

    def create_inventory_item_operation(
        self, *, sequence, barcode="ART-NEW-API", article_id=None,
    ):
        article_id = str(article_id or uuid4())
        return {
            "operation_id": str(uuid4()),
            "operation_type": "CreateInventoryItem",
            "aggregate_type": "INVENTORY_ITEM",
            "aggregate_id": article_id,
            "expected_version": 0,
            "actor_id": "office-test",
            "device_id": "OFFICE-PC-TEST",
            "device_sequence": sequence,
            "occurred_at": "2026-09-12T10:00:00+02:00",
            "payload": {
                "article_id": article_id, "main_barcode": barcode,
                "description": "Nov centralni artikel",
                "track_mode": "QUANTITY",
            },
            "contract_version": 1,
        }

    def update_inventory_price_operation(
        self, article_id, *, sequence, expected_version, price, reason="Nov cenik",
    ):
        return {
            "operation_id": str(uuid4()),
            "operation_type": "UpdateInventorySalePrice",
            "aggregate_type": "INVENTORY_ITEM",
            "aggregate_id": str(article_id), "expected_version": expected_version,
            "actor_id": "office-test", "device_id": "OFFICE-PC-TEST",
            "device_sequence": sequence,
            "occurred_at": "2026-09-14T10:00:00+02:00",
            "payload": {
                "article_id": str(article_id),
                "expected_version": expected_version,
                "new_sale_price": price, "reason": reason,
            },
            "contract_version": 1,
        }

    def purchase_receipt_operation(
        self, receipt_id, supplier_id, article_id, *, sequence,
        document_type="DOB", external_no="DOB-SUP-1", quantity="2",
        sale_price="15.00", attachments=None,
    ):
        return {
            "operation_id": str(uuid4()),
            "operation_type": "ConfirmPurchaseReceipt",
            "aggregate_type": "PURCHASE_RECEIPT",
            "aggregate_id": str(receipt_id),
            "expected_version": 0,
            "actor_id": "office-test",
            "device_id": "OFFICE-PC-TEST",
            "device_sequence": sequence,
            "occurred_at": "2026-09-12T10:00:00+02:00",
            "payload": {
                "receipt_id": str(receipt_id),
                "supplier_id": str(supplier_id),
                "supplier_expected_version": 1,
                "document_type": document_type,
                "custom_type_code": "",
                "external_document_no": external_no,
                "document_date": "2026-09-12",
                "received_date": "2026-09-12",
                "currency": "EUR",
                "document_total": "24.20",
                "shipping_total": "2.00",
                "document_review_confirmed": True,
                "invoice_due_date": "2026-10-12" if document_type == "DRA" else "",
                "invoice_payment_status": "NEPLACAN" if document_type == "DRA" else "",
                "lines": [{
                    "line_id": str(uuid4()),
                    "article_id": str(article_id),
                    "quantity": quantity,
                    "net_unit_price": "10.00",
                    "discount_percent": "0",
                    "vat_rate": "11",
                    "supplier_line_total": "22.20",
                    "confirmed_sale_price": sale_price,
                    "supplier_barcode": "SUP-BARCODE-1",
                    "lot_serial": "",
                }],
                "attachments": attachments or [],
            },
            "contract_version": 1,
        }

    def seed_supplier_and_article(
        self, *, quantity="0", sale_price="0", track_mode="QUANTITY",
    ):
        from datetime import datetime, timezone
        from sqlalchemy import insert
        from mikrofix_server.models import inventory_items, partners

        supplier_id, article_id = uuid4(), uuid4()
        with self.engine.begin() as connection:
            connection.execute(insert(partners).values(
                partner_uuid=supplier_id, name="Dobavitelj d.o.o.",
                row_version=1, active=True, verified_by="office-test",
                verified_at=datetime.now(timezone.utc),
            ))
            connection.execute(insert(inventory_items).values(
                article_uuid=article_id, barcode=f"MAIN-{article_id}",
                description="Testni artikel", quantity_on_hand=quantity,
                current_sale_price=sale_price, track_mode=track_mode,
            ))
        return supplier_id, article_id

    def prepared_draft(self, *, sequence, barcode, order_no=None):
        from sqlalchemy import insert
        from mikrofix_server.models import inventory_items

        order = self.operation(sequence=sequence)
        if order_no is not None:
            order["payload"]["order_no"] = order_no
        created = self.client.post(
            "/v1/sync/operations", json=order, headers=self.headers
        )
        self.assertEqual(created.json()["result_code"], "ACCEPTED")
        article_id = uuid4()
        with self.engine.begin() as connection:
            connection.execute(insert(inventory_items).values(
                article_uuid=article_id, barcode=barcode,
                description="Testni del", quantity_on_hand=1,
            ))
        completion = self.completion_operation(
            order, sequence=sequence + 1, barcode=barcode, article_id=article_id
        )
        completed = self.client.post(
            "/v1/sync/operations", json=completion, headers=self.headers
        )
        self.assertEqual(completed.json()["result_code"], "ACCEPTED")
        return order, completed.json()["result"]["draft_uuid"]

    def central_login(self, *, role="CASHIER", application="CASHIER"):
        from mikrofix_server.auth import create_central_user, register_workstation

        with self.engine.begin() as connection:
            user_id = create_central_user(
                connection,
                username=f"{role.lower()}-auth",
                display_name=f"{role.title()} Auth",
                role=role,
                pin="246810",
                operator_tax_number="12345678" if role == "CASHIER" else "",
                must_change_pin=False,
            )
            _, secret = register_workstation(
                connection,
                workstation_code=f"{application}-PC-{role}",
                display_name=f"{application.title()} PC",
                application_type=application,
                approved_by=user_id,
            )
        response = self.client.post("/v1/auth/login", json={
            "username": f"{role.lower()}-auth",
            "pin": "246810",
            "workstation_code": f"{application}-PC-{role}",
            "workstation_secret": secret,
            "application_type": application,
        })
        return response

    def test_central_session_derives_actor_and_workstation(self):
        from sqlalchemy import select
        from mikrofix_server.models import accepted_operations, invoices

        login_response = self.central_login()
        self.assertEqual(login_response.status_code, 200, login_response.text)
        session_headers = {
            "Authorization": f"Bearer {login_response.json()['access_token']}"
        }
        _, draft_id = self.prepared_draft(sequence=1, barcode="AUTH-PART-1")
        issue = self.issue_operation(draft_id, sequence=1)
        issue["actor_id"] = "forged-actor"
        issue["device_id"] = "FORGED-PC"

        response = self.client.post(
            "/v1/sync/operations", json=issue, headers=session_headers
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result_code"], "ACCEPTED")
        with self.engine.connect() as connection:
            operation = connection.execute(
                select(accepted_operations).where(
                    accepted_operations.c.operation_id == issue["operation_id"]
                )
            ).one()
            invoice = connection.execute(select(invoices)).one()
        self.assertEqual(operation.actor_id, "cashier-auth")
        self.assertEqual(operation.device_id, "CASHIER-PC-CASHIER")
        self.assertEqual(invoice.cashier_id, "cashier-auth")
        self.assertEqual(invoice.operator_tax_number, "12345678")
        self.assertEqual(invoice.snapshot_json["operator_tax_number"], "12345678")

    def test_session_is_bound_to_application_permission_and_can_logout(self):
        denied = self.central_login(role="SERVICE", application="CASHIER")
        self.assertEqual(denied.status_code, 403)

        accepted = self.central_login()
        token = accepted.json()["access_token"]
        session_headers = {"Authorization": f"Bearer {token}"}
        self.assertEqual(
            self.client.get("/v1/cashier/partners", headers=session_headers).status_code,
            200,
        )
        self.assertEqual(
            self.client.get("/v1/sync/changes", headers=session_headers).status_code,
            403,
        )
        self.assertEqual(
            self.client.post("/v1/auth/logout", headers=session_headers).status_code,
            204,
        )
        self.assertEqual(
            self.client.get("/v1/cashier/partners", headers=session_headers).status_code,
            401,
        )

    def test_signed_offline_service_preserves_original_actor_and_transmitter(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from sqlalchemy import func, select

        from mikrofix_device_signing import sign_operation
        from mikrofix_server.auth import (
            create_central_user, register_workstation, update_user_access,
        )
        from mikrofix_server.models import accepted_operations
        from mikrofix_sync_contracts import SyncOperationEnvelope

        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        with self.engine.begin() as connection:
            original_actor = create_central_user(
                connection, username="serviser-a", display_name="Serviser A",
                role="SERVICE", pin="246810", must_change_pin=False,
            )
            transmitter = create_central_user(
                connection, username="serviser-b", display_name="Serviser B",
                role="SERVICE", pin="135790", must_change_pin=False,
            )
            _, station_secret = register_workstation(
                connection, workstation_code="SERVICE-PC-SIGNED",
                display_name="Podpisani Service PC", application_type="SERVICE",
                approved_by=original_actor, signing_public_key=public_key,
            )
        login_response = self.client.post("/v1/auth/login", json={
            "username": "serviser-b", "pin": "135790",
            "workstation_code": "SERVICE-PC-SIGNED",
            "workstation_secret": station_secret, "application_type": "SERVICE",
        })
        self.assertEqual(login_response.status_code, 200, login_response.text)
        session_headers = {
            "Authorization": f"Bearer {login_response.json()['access_token']}"
        }
        raw = self.operation(sequence=1)
        raw.update({
            "actor_id": "serviser-a",
            "actor_user_id": str(original_actor),
            "actor_authorization_version": 1,
            "device_id": "SERVICE-PC-SIGNED",
        })
        signed = sign_operation(SyncOperationEnvelope(**raw), private_key)
        with self.engine.begin() as connection:
            update_user_access(
                connection, user_uuid=original_actor, display_name="Serviser A",
                role="SERVICE", permissions=set(), is_active=False,
                operator_tax_number="", changed_by=transmitter,
                expected_version=1,
            )

        accepted = self.client.post(
            "/v1/sync/operations", json=asdict(signed), headers=session_headers
        )

        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["result_code"], "ACCEPTED")
        with self.engine.connect() as connection:
            stored = connection.execute(
                select(accepted_operations).where(
                    accepted_operations.c.operation_id == signed.operation_id
                )
            ).one()
        self.assertEqual(stored.actor_id, "serviser-a")
        self.assertEqual(stored.actor_user_uuid, original_actor)
        self.assertEqual(stored.transmitted_by_user_uuid, transmitter)
        self.assertEqual(stored.device_id, "SERVICE-PC-SIGNED")

        tampered_raw = self.operation(sequence=2)
        tampered_raw.update({
            "actor_id": "serviser-a",
            "actor_user_id": str(original_actor),
            "actor_authorization_version": 1,
            "device_id": "SERVICE-PC-SIGNED",
        })
        tampered = asdict(sign_operation(
            SyncOperationEnvelope(**tampered_raw), private_key
        ))
        tampered["payload"]["customer_name"] = "Spremenjeno po podpisu"
        rejected = self.client.post(
            "/v1/sync/operations", json=tampered, headers=session_headers
        )
        self.assertEqual(rejected.status_code, 403)
        with self.engine.connect() as connection:
            count = connection.execute(
                select(func.count()).select_from(accepted_operations).where(
                    accepted_operations.c.operation_id == tampered["operation_id"]
                )
            ).scalar_one()
        self.assertEqual(count, 0)

    def test_admin_registers_cashier_and_temporary_pin_blocks_business_use(self):
        admin_login = self.central_login(role="ADMIN", application="OFFICE")
        self.assertEqual(admin_login.status_code, 200, admin_login.text)
        admin_headers = {
            "Authorization": f"Bearer {admin_login.json()['access_token']}"
        }
        user_response = self.client.post(
            "/v1/admin/users",
            headers=admin_headers,
            json={
                "username": "nova-blagajna",
                "display_name": "Nova blagajna",
                "role": "CASHIER",
                "pin": "975310",
                "operator_tax_number": "87654321",
            },
        )
        self.assertEqual(user_response.status_code, 201, user_response.text)
        station_response = self.client.post(
            "/v1/admin/workstations",
            headers=admin_headers,
            json={
                "workstation_code": "POS-PC-NOVA",
                "display_name": "Nova blagajna PC",
                "application_type": "CASHIER",
            },
        )
        self.assertEqual(station_response.status_code, 201, station_response.text)
        secret = station_response.json()["workstation_secret"]
        login_request = {
            "username": "nova-blagajna", "pin": "975310",
            "workstation_code": "POS-PC-NOVA", "workstation_secret": secret,
            "application_type": "CASHIER",
        }
        first_login = self.client.post("/v1/auth/login", json=login_request)
        self.assertTrue(first_login.json()["must_change_pin"])
        first_headers = {
            "Authorization": f"Bearer {first_login.json()['access_token']}"
        }
        self.assertEqual(
            self.client.get("/v1/cashier/partners", headers=first_headers).status_code,
            403,
        )
        changed = self.client.post(
            "/v1/auth/change-pin", headers=first_headers,
            json={"current_pin": "975310", "new_pin": "864209"},
        )
        self.assertEqual(changed.status_code, 204, changed.text)
        self.assertEqual(
            self.client.get("/v1/cashier/partners", headers=first_headers).status_code,
            401,
        )
        login_request["pin"] = "864209"
        second_login = self.client.post("/v1/auth/login", json=login_request)
        self.assertEqual(second_login.status_code, 200, second_login.text)
        self.assertFalse(second_login.json()["must_change_pin"])
        second_headers = {
            "Authorization": f"Bearer {second_login.json()['access_token']}"
        }
        self.assertEqual(
            self.client.get("/v1/cashier/partners", headers=second_headers).status_code,
            200,
        )

    def test_health_and_authentication_boundary(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(
            self.client.post("/v1/sync/operations", json=self.operation()).status_code,
            401,
        )
        self.assertEqual(
            self.client.get("/v1/admin/users", headers=self.headers).status_code,
            403,
        )

    def test_create_retry_and_pull_share_one_central_event(self):
        request = self.operation()
        first = self.client.post(
            "/v1/sync/operations", json=request, headers=self.headers
        )
        second = self.client.post(
            "/v1/sync/operations", json=request, headers=self.headers
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(first.json()["result_code"], "ACCEPTED")

        page = self.client.get(
            "/v1/sync/changes?after=0", headers=self.headers
        )
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(len(page.json()["changes"]), 1)
        self.assertEqual(
            page.json()["changes"][0]["aggregate_id"], request["aggregate_id"]
        )

    def test_same_operation_id_with_changed_request_is_protocol_conflict(self):
        request = self.operation()
        self.assertEqual(
            self.client.post(
                "/v1/sync/operations", json=request, headers=self.headers
            ).status_code,
            200,
        )
        changed = deepcopy(request)
        changed["payload"]["customer_name"] = "Druga stranka"
        response = self.client.post(
            "/v1/sync/operations", json=changed, headers=self.headers
        )
        self.assertEqual(response.status_code, 409, response.text)

    def test_device_sequence_cannot_identify_two_operations(self):
        first = self.operation(sequence=7)
        second = self.operation(sequence=7)
        self.assertEqual(
            self.client.post(
                "/v1/sync/operations", json=first, headers=self.headers
            ).status_code,
            200,
        )
        response = self.client.post(
            "/v1/sync/operations", json=second, headers=self.headers
        )
        self.assertEqual(response.status_code, 409, response.text)

    def test_business_rejection_is_a_stable_receipt(self):
        request = self.operation()
        request["expected_version"] = 2
        first = self.client.post(
            "/v1/sync/operations", json=request, headers=self.headers
        )
        second = self.client.post(
            "/v1/sync/operations", json=request, headers=self.headers
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(first.json()["result_code"], "REJECTED")
        self.assertEqual(first.json()["error_code"], "VERSION_CONFLICT")

    def test_invalid_business_payload_is_rejected_before_insert(self):
        from sqlalchemy import func, select

        from mikrofix_server.models import service_orders

        request = self.operation(sequence=9)
        request["payload"]["customer_phone"] = ""
        response = self.client.post(
            "/v1/sync/operations", json=request, headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result_code"], "REJECTED")
        self.assertEqual(response.json()["error_code"], "INVALID_PAYLOAD")
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.execute(select(func.count()).select_from(service_orders)).scalar_one(),
                0,
            )

    def test_sqlite_outbox_reaches_postgres_and_returns_to_inbox(self):
        import sqlite3

        from mikrofix_sync_client import synchronize_once
        from mikrofix_sync_contracts import SyncOperationEnvelope
        from mikrofix_sync_store import (
            enqueue_sync_operation,
            ensure_sync_store_schema,
            outbox_operation,
        )

        class TestClientTransport:
            def __init__(self, client, headers):
                self.client = client
                self.headers = headers

            def submit_operation(self, operation):
                response = self.client.post(
                    "/v1/sync/operations", json=operation, headers=self.headers
                )
                response.raise_for_status()
                return response.json()

            def pull_changes(self, *, after, limit):
                response = self.client.get(
                    "/v1/sync/changes",
                    params={"after": after, "limit": limit},
                    headers=self.headers,
                )
                response.raise_for_status()
                return response.json()

            def pull_inventory_snapshot(self):
                response = self.client.get(
                    "/v1/service/inventory-snapshot", headers=self.headers
                )
                response.raise_for_status()
                return response.json()

            def pull_service_return_snapshot(self):
                response = self.client.get(
                    "/v1/service/returnable-parts-snapshot",
                    headers=self.headers,
                )
                response.raise_for_status()
                return response.json()

        connection = sqlite3.connect(":memory:")
        try:
            ensure_sync_store_schema(connection)
            request = self.operation(sequence=11)
            envelope = SyncOperationEnvelope(**request)
            enqueue_sync_operation(connection, envelope)
            connection.commit()

            result = synchronize_once(
                connection,
                TestClientTransport(self.client, self.headers),
                request["device_id"],
            )
            self.assertFalse(result.interrupted)
            self.assertEqual(result.sent, 1)
            self.assertEqual(result.received, 1)
            self.assertGreater(result.checkpoint, 0)
            self.assertEqual(
                outbox_operation(connection, request["operation_id"])["status"],
                "ACKNOWLEDGED",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT aggregate_uuid FROM sync_inbox_v32"
                ).fetchone()[0],
                request["aggregate_id"],
            )
        finally:
            connection.close()

    def test_technical_completion_consumes_once_and_creates_one_ready_draft(self):
        from sqlalchemy import func, insert, select

        from mikrofix_server.models import (
            inventory_items, invoice_drafts, service_orders,
            service_part_consumptions, technical_completions,
        )

        order = self.operation(sequence=21)
        created = self.client.post(
            "/v1/sync/operations", json=order, headers=self.headers
        )
        self.assertEqual(created.json()["result_code"], "ACCEPTED")
        article_uuid = uuid4()
        with self.engine.begin() as connection:
            connection.execute(insert(inventory_items).values(
                article_uuid=article_uuid, barcode="PART-1",
                description="Testni del", quantity_on_hand=2,
            ))

        completion = self.completion_operation(
            order, sequence=22, article_id=article_uuid
        )
        first = self.client.post(
            "/v1/sync/operations", json=completion, headers=self.headers
        )
        second = self.client.post(
            "/v1/sync/operations", json=completion, headers=self.headers
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(first.json()["result_code"], "ACCEPTED")
        self.assertTrue(first.json()["result"]["draft_uuid"])

        with self.engine.connect() as connection:
            stock = connection.execute(
                select(inventory_items.c.quantity_on_hand).where(
                    inventory_items.c.barcode == "PART-1"
                )
            ).scalar_one()
            self.assertEqual(float(stock), 1.0)
            self.assertEqual(connection.execute(
                select(func.count()).select_from(service_part_consumptions)
            ).scalar_one(), 1)
            self.assertEqual(connection.execute(
                select(func.count()).select_from(technical_completions)
            ).scalar_one(), 1)
            self.assertEqual(connection.execute(
                select(func.count()).select_from(invoice_drafts)
            ).scalar_one(), 1)
            self.assertEqual(connection.execute(
                select(service_orders.c.row_version).where(
                    service_orders.c.sync_uuid == order["aggregate_id"]
                )
            ).scalar_one(), 2)

    def test_impossible_offline_consumption_blocks_item_without_partial_posting(self):
        from sqlalchemy import func, insert, select

        from mikrofix_server.models import (
            change_feed, inventory_items, invoice_drafts,
            service_part_consumptions, technical_completions,
        )

        order = self.operation(sequence=31)
        self.client.post("/v1/sync/operations", json=order, headers=self.headers)
        article_uuid = uuid4()
        with self.engine.begin() as connection:
            connection.execute(insert(inventory_items).values(
                article_uuid=article_uuid, barcode="LAST-ONE",
                description="Zadnji kos", quantity_on_hand=0,
            ))
        completion = self.completion_operation(
            order, sequence=32, barcode="LAST-ONE", article_id=article_uuid
        )
        response = self.client.post(
            "/v1/sync/operations", json=completion, headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result_code"], "REJECTED")
        self.assertEqual(response.json()["error_code"], "OFFLINE_STOCK_CONFLICT")
        self.assertEqual(
            response.json()["result"]["conflicting_barcodes"], ["LAST-ONE"]
        )

        with self.engine.connect() as connection:
            item = connection.execute(
                select(
                    inventory_items.c.quantity_on_hand,
                    inventory_items.c.blocked,
                    inventory_items.c.blocked_reason,
                ).where(inventory_items.c.barcode == "LAST-ONE")
            ).one()
            self.assertEqual(float(item.quantity_on_hand), 0.0)
            self.assertTrue(item.blocked)
            self.assertIn("OFFLINE_CONSUMPTION_CONFLICT", item.blocked_reason)
            for table in (
                service_part_consumptions, technical_completions, invoice_drafts,
            ):
                self.assertEqual(connection.execute(
                    select(func.count()).select_from(table)
                ).scalar_one(), 0)
            self.assertEqual(connection.execute(
                select(func.count()).select_from(change_feed)
            ).scalar_one(), 1)

    def test_two_simultaneous_completions_cannot_consume_the_same_last_unit(self):
        from sqlalchemy import func, insert, select

        from mikrofix_server.models import (
            inventory_items, invoice_drafts, service_part_consumptions,
        )

        first_order = self.operation(sequence=41)
        second_order = self.operation(sequence=42)
        self.client.post(
            "/v1/sync/operations", json=first_order, headers=self.headers
        )
        self.client.post(
            "/v1/sync/operations", json=second_order, headers=self.headers
        )
        article_uuid = uuid4()
        with self.engine.begin() as connection:
            connection.execute(insert(inventory_items).values(
                article_uuid=article_uuid, barcode="ONLY-ONE",
                description="En sam kos", quantity_on_hand=1,
            ))
        first_completion = self.completion_operation(
            first_order, sequence=43, barcode="ONLY-ONE", article_id=article_uuid
        )
        second_completion = self.completion_operation(
            second_order, sequence=44, barcode="ONLY-ONE", article_id=article_uuid
        )

        def submit(payload):
            return self.client.post(
                "/v1/sync/operations", json=payload, headers=self.headers
            ).json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(
                submit, (first_completion, second_completion)
            ))
        self.assertEqual(
            sorted(row["result_code"] for row in receipts),
            ["ACCEPTED", "REJECTED"],
        )
        rejected = next(row for row in receipts if row["result_code"] == "REJECTED")
        self.assertEqual(rejected["error_code"], "OFFLINE_STOCK_CONFLICT")
        with self.engine.connect() as connection:
            self.assertEqual(float(connection.execute(
                select(inventory_items.c.quantity_on_hand).where(
                    inventory_items.c.barcode == "ONLY-ONE"
                )
            ).scalar_one()), 0.0)
            self.assertEqual(connection.execute(
                select(func.count()).select_from(service_part_consumptions)
            ).scalar_one(), 1)
            self.assertEqual(connection.execute(
                select(func.count()).select_from(invoice_drafts)
            ).scalar_one(), 1)

    def test_cashier_lists_locked_ready_service_draft_without_claiming_it(self):
        order, draft_id = self.prepared_draft(
            sequence=51, barcode="LIST-PART", order_no="MF-SN-2026-0051",
        )
        first = self.client.get(
            "/v1/cashier/service-invoice-drafts", headers=self.headers
        )
        second = self.client.get(
            "/v1/cashier/service-invoice-drafts", headers=self.headers
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), second.json())
        draft = first.json()["drafts"][0]
        self.assertEqual(draft["draft_id"], draft_id)
        self.assertEqual(draft["status"], "READY")
        self.assertEqual(draft["aggregate_version"], 1)
        self.assertEqual(
            draft["locked_payload"]["service_order_id"], order["aggregate_id"]
        )
        self.assertEqual(
            draft["locked_payload"]["service_no"], "MF-SN-2026-0051"
        )
        self.assertEqual(draft["locked_payload"]["customer"]["name"], "Testna stranka")

    def test_bank_transfer_issues_once_and_billing_closes_service(self):
        from sqlalchemy import func, select
        from mikrofix_server.models import (
            fiscal_outbox, invoice_drafts, invoice_number_sequences,
            invoices, service_orders,
        )

        order, draft_id = self.prepared_draft(sequence=61, barcode="BANK-PART")
        issue = self.issue_operation(draft_id, sequence=63)
        first = self.client.post(
            "/v1/sync/operations", json=issue, headers=self.headers
        )
        second = self.client.post(
            "/v1/sync/operations", json=issue, headers=self.headers
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(first.json()["result_code"], "ACCEPTED")
        self.assertEqual(first.json()["result"]["invoice_status"], "ISSUED")
        self.assertEqual(first.json()["result"]["payment_status"], "UNPAID")
        self.assertEqual(first.json()["result"]["total_amount"], "55.00")
        self.assertRegex(first.json()["result"]["invoice_number"], r"^MFD-PIS-1$")

        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(
                select(func.count()).select_from(invoices)
            ).scalar_one(), 1)
            self.assertEqual(connection.execute(
                select(func.count()).select_from(invoice_number_sequences)
            ).scalar_one(), 1)
            self.assertEqual(connection.execute(
                select(func.count()).select_from(fiscal_outbox)
            ).scalar_one(), 0)
            self.assertEqual(connection.execute(
                select(invoice_drafts.c.status).where(
                    invoice_drafts.c.draft_uuid == draft_id
                )
            ).scalar_one(), "INVOICED")
            service = connection.execute(select(service_orders).where(
                service_orders.c.sync_uuid == order["aggregate_id"]
            )).one()
            self.assertEqual(service.row_version, 3)
            self.assertEqual(service.payload_json["technical_status"], "BILLING_COMPLETED")
        invoice_id = first.json()["result"]["invoice_id"]
        recovered = self.client.get(
            f"/v1/cashier/invoices/{invoice_id}", headers=self.headers
        ).json()
        self.assertEqual(recovered["snapshot"]["source_type"], "SERVICE")
        self.assertEqual(len(recovered["snapshot"]["lines"]), 2)
        self.assertEqual(
            sum(Decimal(line["line_total"]) for line in recovered["snapshot"]["lines"]),
            Decimal("55.00"),
        )
        history = self.client.get(
            "/v1/cashier/invoices?q=PIS-1&invoice_status=ISSUED",
            headers=self.headers,
        )
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(len(history.json()["invoices"]), 1)
        self.assertEqual(history.json()["invoices"][0]["invoice_id"], invoice_id)
        for query in (
            "Testna stranka",
            "Testni del",
            "BANK-PART",
            order["aggregate_id"],
            str(recovered["issued_at"])[:10],
        ):
            searched = self.client.get(
                "/v1/cashier/invoices", params={"q": query}, headers=self.headers,
            )
            self.assertEqual(searched.status_code, 200, searched.text)
            self.assertEqual(
                [row["invoice_id"] for row in searched.json()["invoices"]],
                [invoice_id],
            )

        office_history = self.client.get(
            "/v1/office/invoices", params={"q": "BANK-PART"}, headers=self.headers,
        )
        self.assertEqual(office_history.status_code, 200, office_history.text)
        self.assertEqual(office_history.json()["invoices"][0]["invoice_id"], invoice_id)

    def test_cash_invoice_is_queued_once_and_does_not_close_before_furs(self):
        from sqlalchemy import func, select
        from mikrofix_server.models import (
            cash_journal_entries, cash_sessions, fiscal_outbox,
            invoice_drafts, invoices, service_orders,
        )

        order, draft_id = self.prepared_draft(sequence=71, barcode="CASH-PART")
        opened = self.client.post(
            "/v1/sync/operations", json=self.open_cash_operation(sequence=72),
            headers=self.headers,
        )
        self.assertEqual(opened.json()["result_code"], "ACCEPTED")
        issue = self.issue_operation(
            draft_id, sequence=73, method="CASH", confirmed=True
        )
        response = self.client.post(
            "/v1/sync/operations", json=issue, headers=self.headers
        )
        self.assertEqual(response.json()["result_code"], "ACCEPTED")
        self.assertEqual(
            response.json()["result"]["invoice_status"], "PENDING_FISCALIZATION"
        )
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(
                select(func.count()).select_from(invoices)
            ).scalar_one(), 1)
            self.assertEqual(connection.execute(
                select(func.count()).select_from(fiscal_outbox)
            ).scalar_one(), 1)
            entry = connection.execute(select(cash_journal_entries)).one()
            invoice = connection.execute(select(invoices)).one()
            session = connection.execute(select(cash_sessions)).one()
            self.assertEqual(str(entry.amount), "55.00")
            self.assertEqual(entry.entry_type, "CASH_SALE")
            self.assertEqual(entry.reference_uuid, invoice.invoice_uuid)
            self.assertEqual(invoice.cash_session_uuid, session.session_uuid)
            self.assertEqual(connection.execute(
                select(invoice_drafts.c.status).where(
                    invoice_drafts.c.draft_uuid == draft_id
                )
            ).scalar_one(), "ISSUING")
            service = connection.execute(select(service_orders).where(
                service_orders.c.sync_uuid == order["aggregate_id"]
            )).one()
            self.assertEqual(service.row_version, 2)
            self.assertEqual(service.payload_json["technical_status"], "READY_FOR_INVOICE")

    def test_cash_payment_without_open_session_changes_nothing(self):
        from sqlalchemy import func, select
        from mikrofix_server.models import (
            cash_journal_entries, invoice_drafts, invoice_number_sequences, invoices,
        )

        _, draft_id = self.prepared_draft(sequence=74, barcode="NO-CASH-SESSION")
        response = self.client.post(
            "/v1/sync/operations",
            json=self.issue_operation(
                draft_id, sequence=76, method="CASH", confirmed=True
            ),
            headers=self.headers,
        ).json()
        self.assertEqual(response["result_code"], "REJECTED")
        self.assertEqual(response["error_code"], "CASH_SESSION_REQUIRED")
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(
                select(func.count()).select_from(invoices)
            ).scalar_one(), 0)
            self.assertEqual(connection.execute(
                select(func.count()).select_from(invoice_number_sequences)
            ).scalar_one(), 0)
            self.assertEqual(connection.execute(
                select(func.count()).select_from(cash_journal_entries)
            ).scalar_one(), 0)
            self.assertEqual(connection.execute(select(
                invoice_drafts.c.status
            )).scalar_one(), "READY")

    def test_late_trr_confirmation_marks_same_invoice_paid_without_cash_or_furs(self):
        from sqlalchemy import func, select
        from mikrofix_server.models import cash_journal_entries, fiscal_outbox, invoices

        _, draft_id = self.prepared_draft(sequence=270, barcode="LATE-TRR")
        issued = self.client.post(
            "/v1/sync/operations",
            json=self.issue_operation(draft_id, sequence=271),
            headers=self.headers,
        ).json()
        invoice_id = issued["result"]["invoice_id"]
        settlement = self.settle_bank_invoice_operation(
            invoice_id, sequence=272, method="BANK_TRANSFER"
        )
        first = self.client.post(
            "/v1/sync/operations", json=settlement, headers=self.headers
        ).json()
        repeated = self.client.post(
            "/v1/sync/operations", json=settlement, headers=self.headers
        ).json()
        self.assertEqual(first, repeated)
        self.assertEqual(first["result"]["invoice_id"], invoice_id)
        self.assertEqual(first["result"]["invoice_status"], "ISSUED")
        self.assertEqual(first["result"]["payment_received_method"], "BANK_TRANSFER")
        with self.engine.connect() as connection:
            invoice = connection.execute(select(invoices)).one()
            self.assertEqual(invoice.payment_status, "PAID")
            self.assertEqual(invoice.payment_received_method, "BANK_TRANSFER")
            self.assertIsNotNone(invoice.payment_received_at)
            self.assertEqual(connection.execute(
                select(func.count()).select_from(cash_journal_entries)
            ).scalar_one(), 0)
            self.assertEqual(connection.execute(
                select(func.count()).select_from(fiscal_outbox)
            ).scalar_one(), 0)

    def test_late_cash_payment_books_once_and_queues_subsequent_furs(self):
        from sqlalchemy import func, select
        from mikrofix_server.fiscal_worker import process_fiscal_invoice
        from mikrofix_server.models import cash_journal_entries, fiscal_outbox, invoices

        class ConfirmingProvider:
            def __init__(self):
                self.requests = []
                self.submissions = 0

            def prepare_request(self, request, *, message_id):
                self.requests.append(dict(request))
                return {"InvoiceRequest": {
                    "Header": {"MessageID": message_id},
                    "Invoice": {"ProtectedID": "c" * 32},
                }}

            def submit_prepared(self, _payload):
                self.submissions += 1
                return {
                    "status": "CONFIRMED", "zoi": "c" * 32,
                    "eor": "EOR-LATE-CASH-1", "response": {"ok": True},
                }

        _, draft_id = self.prepared_draft(sequence=273, barcode="LATE-CASH")
        issued = self.client.post(
            "/v1/sync/operations",
            json=self.issue_operation(draft_id, sequence=274),
            headers=self.headers,
        ).json()
        invoice_id = issued["result"]["invoice_id"]
        opened = self.client.post(
            "/v1/sync/operations", json=self.open_cash_operation(sequence=275),
            headers=self.headers,
        ).json()
        self.assertEqual(opened["result_code"], "ACCEPTED")
        settled = self.client.post(
            "/v1/sync/operations",
            json=self.settle_bank_invoice_operation(
                invoice_id, sequence=276, method="CASH"
            ), headers=self.headers,
        ).json()
        self.assertEqual(settled["result_code"], "ACCEPTED", settled)
        self.assertEqual(settled["result"]["invoice_status"], "PENDING_FISCALIZATION")
        second = self.client.post(
            "/v1/sync/operations",
            json=self.settle_bank_invoice_operation(
                invoice_id, sequence=277, method="CASH"
            ), headers=self.headers,
        ).json()
        self.assertEqual(second["result_code"], "REJECTED")
        self.assertEqual(second["error_code"], "INVOICE_NOT_UNPAID")

        provider = ConfirmingProvider()
        confirmed = process_fiscal_invoice(self.engine, invoice_id, provider)
        repeated = process_fiscal_invoice(self.engine, invoice_id, provider)
        self.assertEqual(confirmed, repeated)
        self.assertTrue(provider.requests[0]["subsequent_submit"])
        self.assertEqual(provider.submissions, 1)
        with self.engine.connect() as connection:
            invoice = connection.execute(select(invoices)).one()
            entry = connection.execute(select(cash_journal_entries)).one()
            outbox = connection.execute(select(fiscal_outbox)).one()
            self.assertEqual(invoice.furs_eor, "EOR-LATE-CASH-1")
            self.assertEqual(invoice.payment_received_method, "CASH")
            self.assertIsNotNone(invoice.fiscal_submission_deadline)
            self.assertEqual(entry.reference_type, "INVOICE_LATE_PAYMENT")
            self.assertEqual(str(entry.amount), "55.00")
            self.assertEqual(outbox.status, "CONFIRMED")
            self.assertEqual(connection.execute(
                select(func.count()).select_from(cash_journal_entries)
            ).scalar_one(), 1)

    def test_central_cash_session_manual_entries_and_close_are_idempotent(self):
        session_id = str(uuid4())
        opening = self.open_cash_operation(
            sequence=77, session_id=session_id, opening="100"
        )
        first = self.client.post(
            "/v1/sync/operations", json=opening, headers=self.headers
        ).json()
        repeated = self.client.post(
            "/v1/sync/operations", json=opening, headers=self.headers
        ).json()
        self.assertEqual(first, repeated)
        self.assertEqual(first["result"]["session_no"], "BD-2026-0001")

        receipt = self.client.post(
            "/v1/sync/operations",
            json=self.manual_cash_operation(
                session_id, sequence=78, direction="RECEIPT", amount="25"
            ), headers=self.headers,
        ).json()
        expense = self.client.post(
            "/v1/sync/operations",
            json=self.manual_cash_operation(
                session_id, sequence=79, direction="EXPENSE", amount="10"
            ), headers=self.headers,
        ).json()
        self.assertEqual(receipt["result"]["balance_after"], "125.00")
        self.assertEqual(expense["result"]["balance_after"], "115.00")

        excessive = self.client.post(
            "/v1/sync/operations",
            json=self.manual_cash_operation(
                session_id, sequence=80, direction="EXPENSE", amount="116"
            ), headers=self.headers,
        ).json()
        self.assertEqual(excessive["error_code"], "NEGATIVE_CASH_BALANCE")

        close = self.close_cash_operation(
            session_id, sequence=81, counted="114.50", note="Popis"
        )
        closed = self.client.post(
            "/v1/sync/operations", json=close, headers=self.headers
        ).json()
        closed_again = self.client.post(
            "/v1/sync/operations", json=close, headers=self.headers
        ).json()
        self.assertEqual(closed, closed_again)
        self.assertEqual(closed["result"]["expected_cash"], "115.00")
        self.assertEqual(closed["result"]["difference"], "-0.50")

        detail = self.client.get(
            f"/v1/cashier/cash-sessions/{session_id}", headers=self.headers
        ).json()
        self.assertEqual(detail["status"], "CLOSED")
        self.assertEqual(len(detail["entries"]), 2)
        current = self.client.get(
            "/v1/cashier/cash-sessions/current",
            params={"premise_id": "MFD", "electronic_device_id": "PIS"},
            headers=self.headers,
        ).json()
        self.assertIsNone(current["session"])

    def test_two_cashiers_cannot_open_two_sessions_for_one_device(self):
        requests = (
            self.open_cash_operation(sequence=82),
            self.open_cash_operation(sequence=83),
        )

        def submit(payload):
            return self.client.post(
                "/v1/sync/operations", json=payload, headers=self.headers
            ).json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(submit, requests))
        self.assertEqual(
            sorted(row["result_code"] for row in receipts),
            ["ACCEPTED", "REJECTED"],
        )
        rejected = next(row for row in receipts if row["result_code"] == "REJECTED")
        self.assertEqual(rejected["error_code"], "CASH_SESSION_ALREADY_OPEN")

    def test_simultaneous_cash_entries_have_one_ordered_balance(self):
        session_id = str(uuid4())
        self.assertEqual(self.client.post(
            "/v1/sync/operations",
            json=self.open_cash_operation(
                sequence=84, session_id=session_id, opening="0"
            ), headers=self.headers,
        ).json()["result_code"], "ACCEPTED")
        requests = (
            self.manual_cash_operation(
                session_id, sequence=85, direction="RECEIPT", amount="10",
                reference="BP-10",
            ),
            self.manual_cash_operation(
                session_id, sequence=86, direction="RECEIPT", amount="20",
                reference="BP-20",
            ),
        )

        def submit(payload):
            return self.client.post(
                "/v1/sync/operations", json=payload, headers=self.headers
            ).json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(submit, requests))
        self.assertTrue(all(row["result_code"] == "ACCEPTED" for row in receipts))
        detail = self.client.get(
            f"/v1/cashier/cash-sessions/{session_id}", headers=self.headers
        ).json()
        self.assertEqual(
            [entry["entry_no"] for entry in detail["entries"]], [1, 2]
        )
        self.assertEqual(detail["entries"][-1]["balance_after"], "30.00")

    def test_two_cashiers_issue_at_most_one_invoice_and_consume_one_number(self):
        from sqlalchemy import func, select
        from mikrofix_server.models import invoice_number_sequences, invoices

        _, draft_id = self.prepared_draft(sequence=81, barcode="RACE-PART")
        requests = (
            self.issue_operation(draft_id, sequence=83, actor="a", device="POS-A"),
            self.issue_operation(draft_id, sequence=84, actor="b", device="POS-B"),
        )

        def submit(payload):
            return self.client.post(
                "/v1/sync/operations", json=payload, headers=self.headers
            ).json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(submit, requests))
        self.assertEqual(
            sorted(row["result_code"] for row in receipts),
            ["ACCEPTED", "REJECTED"],
        )
        rejected = next(row for row in receipts if row["result_code"] == "REJECTED")
        self.assertEqual(rejected["error_code"], "INVOICE_ALREADY_EXISTS")
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(
                select(func.count()).select_from(invoices)
            ).scalar_one(), 1)
            self.assertEqual(connection.execute(
                select(invoice_number_sequences.c.last_number)
            ).scalar_one(), 1)

    def test_invoice_issue_rejects_content_mutation_and_wrong_payment_total(self):
        from sqlalchemy import func, select
        from mikrofix_server.models import invoice_number_sequences, invoices

        _, draft_id = self.prepared_draft(sequence=91, barcode="IMMUTABLE-PART")
        changed = self.issue_operation(draft_id, sequence=93)
        changed["payload"]["discount"] = "10"
        response = self.client.post(
            "/v1/sync/operations", json=changed, headers=self.headers
        ).json()
        self.assertEqual(response["error_code"], "INVALID_PAYLOAD")

        mismatch = self.issue_operation(draft_id, sequence=94)
        mismatch["payload"]["payments"][0]["amount"] = "54.99"
        response = self.client.post(
            "/v1/sync/operations", json=mismatch, headers=self.headers
        ).json()
        self.assertEqual(response["error_code"], "PAYMENT_TOTAL_MISMATCH")
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(
                select(func.count()).select_from(invoices)
            ).scalar_one(), 0)
            self.assertEqual(connection.execute(
                select(func.count()).select_from(invoice_number_sequences)
            ).scalar_one(), 0)

    def test_zero_balance_invoice_is_server_generated_and_does_not_restore_stock(self):
        from sqlalchemy import select
        from mikrofix_server.models import inventory_items, invoices, service_orders

        _, draft_id = self.prepared_draft(sequence=95, barcode="ZERO-PART")
        request = self.issue_operation(
            draft_id, sequence=97, settlement_mode="ZERO_BALANCE"
        )
        response = self.client.post(
            "/v1/sync/operations", json=request, headers=self.headers
        ).json()
        repeated = self.client.post(
            "/v1/sync/operations", json=request, headers=self.headers
        ).json()

        self.assertEqual(response["result_code"], "ACCEPTED")
        self.assertEqual(repeated, response)
        self.assertEqual(response["result"]["total_amount"], "0.00")
        with self.engine.connect() as connection:
            invoice = connection.execute(select(invoices)).one()
            service_order = connection.execute(select(service_orders)).one()
            stock = connection.execute(select(
                inventory_items.c.quantity_on_hand
            ).where(inventory_items.c.barcode == "ZERO-PART")).scalar_one()
        snapshot = dict(invoice.snapshot_json)
        self.assertEqual(snapshot["settlement_mode"], "ZERO_BALANCE")
        self.assertEqual(snapshot["payments"], [])
        self.assertEqual(invoice.status, "ISSUED")
        self.assertEqual(invoice.payment_status, "PAID")
        self.assertEqual(service_order.payload_json["technical_status"], "BILLING_COMPLETED")
        self.assertEqual(len(snapshot["lines"]), 4)
        positive, negative = snapshot["lines"][:2], snapshot["lines"][2:]
        self.assertEqual([line["quantity"] for line in positive], ["1", "1"])
        self.assertEqual([line["quantity"] for line in negative], ["-1", "-1"])
        self.assertEqual([line["line_total"] for line in positive], ["30.00", "25.00"])
        self.assertEqual([line["line_total"] for line in negative], ["-30.00", "-25.00"])
        self.assertEqual(
            [line["adjustment_of_line_id"] for line in negative],
            [line["line_id"] for line in positive],
        )
        self.assertEqual(len({line["line_id"] for line in snapshot["lines"]}), 4)
        self.assertEqual(stock, Decimal("0"))

    def test_uncertain_furs_retry_keeps_one_invoice_and_closes_only_on_confirmation(self):
        from sqlalchemy import func, select
        from mikrofix_server.fiscalization import (
            confirm_fiscal_submission,
            mark_fiscal_submission_uncertain,
            prepare_fiscal_submission,
            start_fiscal_submission,
        )
        from mikrofix_server.models import fiscal_outbox, invoices, service_orders

        order, draft_id = self.prepared_draft(sequence=101, barcode="FURS-PART")
        opened = self.client.post(
            "/v1/sync/operations", json=self.open_cash_operation(sequence=102),
            headers=self.headers,
        )
        self.assertEqual(opened.json()["result_code"], "ACCEPTED")
        issue = self.issue_operation(
            draft_id, sequence=103, method="CASH", confirmed=True
        )
        issued = self.client.post(
            "/v1/sync/operations", json=issue, headers=self.headers
        ).json()
        invoice_id = issued["result"]["invoice_id"]

        with self.engine.begin() as connection:
            queued = connection.execute(select(fiscal_outbox)).one()
            prepared_request = {
                "InvoiceRequest": {
                    "Header": {"MessageID": str(queued.message_uuid)},
                    "Invoice": {"ProtectedID": "a" * 32},
                }
            }
            prepare_fiscal_submission(
                connection, invoice_id, prepared_request=prepared_request
            )
            first_send = start_fiscal_submission(connection, invoice_id)
        with self.engine.begin() as connection:
            uncertain = mark_fiscal_submission_uncertain(
                connection, invoice_id, error="connection result unknown"
            )
        self.assertEqual(uncertain["status"], "FISCAL_RECONCILIATION")
        with self.engine.connect() as connection:
            service = connection.execute(select(service_orders).where(
                service_orders.c.sync_uuid == order["aggregate_id"]
            )).one()
            self.assertEqual(service.row_version, 2)
            self.assertEqual(service.payload_json["technical_status"], "READY_FOR_INVOICE")

        with self.engine.begin() as connection:
            retry = start_fiscal_submission(connection, invoice_id)
        self.assertEqual(retry["request"], first_send["request"])
        self.assertEqual(retry["request_sha256"], first_send["request_sha256"])
        with self.engine.begin() as connection:
            confirmed = confirm_fiscal_submission(
                connection, invoice_id, zoi="a" * 32, eor="EOR-TEST-1",
                response={"InvoiceResponse": {"EOR": "EOR-TEST-1"}},
            )
        with self.engine.begin() as connection:
            repeated = confirm_fiscal_submission(
                connection, invoice_id, zoi="a" * 32, eor="EOR-TEST-1",
                response={"ignored_on_idempotent_repeat": True},
            )
        self.assertEqual(confirmed, repeated)

        with self.engine.connect() as connection:
            invoice = connection.execute(select(invoices).where(
                invoices.c.invoice_uuid == invoice_id
            )).one()
            self.assertEqual(invoice.status, "ISSUED")
            self.assertEqual(invoice.furs_eor, "EOR-TEST-1")
            self.assertEqual(connection.execute(
                select(func.count()).select_from(invoices)
            ).scalar_one(), 1)
            outbox = connection.execute(select(fiscal_outbox)).one()
            self.assertEqual(outbox.status, "CONFIRMED")
            self.assertEqual(outbox.attempt_count, 2)
            service = connection.execute(select(service_orders).where(
                service_orders.c.sync_uuid == order["aggregate_id"]
            )).one()
            self.assertEqual(service.row_version, 3)
            self.assertEqual(service.payload_json["technical_status"], "BILLING_COMPLETED")

    def test_f9_partner_selection_uses_central_versioned_snapshot(self):
        from datetime import datetime, timezone
        from sqlalchemy import insert, select
        from mikrofix_server.models import invoices, partners

        _, draft_id = self.prepared_draft(sequence=111, barcode="PARTNER-PART")
        partner_id = uuid4()
        with self.engine.begin() as connection:
            connection.execute(insert(partners).values(
                partner_uuid=partner_id,
                name="Preverjeno podjetje d.o.o.",
                address="Poslovna cesta 1",
                post_city="1000 Ljubljana",
                tax_number="12345678",
                phone="01 000 00 00",
                email="racuni@example.test",
                row_version=4,
                active=True,
                verified_by="office-a",
                verified_at=datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc),
            ))

        found = self.client.get(
            "/v1/cashier/partners?q=12345678", headers=self.headers
        )
        self.assertEqual(found.status_code, 200, found.text)
        self.assertEqual(found.json()["partners"][0]["row_version"], 4)

        issue = self.issue_operation(draft_id, sequence=113)
        issue["payload"]["billing_customer"] = {
            "source": "CENTRAL_PARTNER",
            "confirmed": True,
            "partner_id": str(partner_id),
            "expected_version": 4,
        }
        response = self.client.post(
            "/v1/sync/operations", json=issue, headers=self.headers
        )
        self.assertEqual(response.json()["result_code"], "ACCEPTED")
        with self.engine.connect() as connection:
            invoice = connection.execute(select(invoices)).one()
            self.assertEqual(invoice.billing_partner_uuid, partner_id)
            self.assertEqual(
                invoice.billing_customer_json["name"],
                "Preverjeno podjetje d.o.o.",
            )
            self.assertEqual(invoice.snapshot_json["draft"]["customer"]["name"], "Testna stranka")
            self.assertEqual(invoice.snapshot_json["billing_customer"]["partner_version"], 4)

    def test_changed_f9_partner_requires_customer_reconfirmation(self):
        from datetime import datetime, timezone
        from sqlalchemy import func, insert, select
        from mikrofix_server.models import invoice_number_sequences, invoices, partners

        _, draft_id = self.prepared_draft(sequence=121, barcode="STALE-PARTNER")
        partner_id = uuid4()
        with self.engine.begin() as connection:
            connection.execute(insert(partners).values(
                partner_uuid=partner_id, name="Spremenjen partner",
                row_version=2, active=True, verified_by="office-a",
                verified_at=datetime.now(timezone.utc),
            ))
        issue = self.issue_operation(draft_id, sequence=123)
        issue["payload"]["billing_customer"] = {
            "source": "CENTRAL_PARTNER", "confirmed": True,
            "partner_id": str(partner_id), "expected_version": 1,
        }
        response = self.client.post(
            "/v1/sync/operations", json=issue, headers=self.headers
        ).json()
        self.assertEqual(response["result_code"], "REJECTED")
        self.assertEqual(response["error_code"], "BILLING_CUSTOMER_CONFLICT")
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(
                select(func.count()).select_from(invoices)
            ).scalar_one(), 0)
            self.assertEqual(connection.execute(
                select(func.count()).select_from(invoice_number_sequences)
            ).scalar_one(), 0)

    def test_f9_creates_new_partner_idempotently_without_update_path(self):
        from sqlalchemy import func, select
        from mikrofix_server.models import partners

        request = self.create_partner_operation(sequence=131)
        first = self.client.post(
            "/v1/sync/operations", json=request, headers=self.headers
        )
        second = self.client.post(
            "/v1/sync/operations", json=request, headers=self.headers
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(first.json()["result_code"], "ACCEPTED")
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(
                select(func.count()).select_from(partners)
            ).scalar_one(), 1)
            partner = connection.execute(select(partners)).one()
            self.assertEqual(partner.row_version, 1)
            self.assertEqual(partner.verification_source, "MANUAL_REVIEW")
            self.assertEqual(partner.verified_by, "blagajnik-a")

    def test_f9_duplicate_tax_number_returns_existing_partner(self):
        from sqlalchemy import func, select
        from mikrofix_server.models import partners

        first = self.create_partner_operation(sequence=141)
        second = self.create_partner_operation(sequence=142)
        self.client.post(
            "/v1/sync/operations", json=first, headers=self.headers
        )
        response = self.client.post(
            "/v1/sync/operations", json=second, headers=self.headers
        ).json()
        self.assertEqual(response["result_code"], "REJECTED")
        self.assertEqual(response["error_code"], "PARTNER_ALREADY_EXISTS")
        self.assertEqual(
            response["result"]["existing_partner_id"], first["aggregate_id"]
        )
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(
                select(func.count()).select_from(partners)
            ).scalar_one(), 1)

    def test_ajpes_lookup_is_only_an_unconfirmed_suggestion(self):
        from sqlalchemy import func, select
        from mikrofix_server.models import partners

        found = SimpleNamespace(
            name="AJPES podjetje d.o.o.", address="Uradna 1",
            post_city="1000 Ljubljana", tax_number="87654321",
            registration_number="8765432000",
        )
        with patch("mikrofix_server.app.find_partner_by_tax_number", return_value=found):
            response = self.client.get(
                "/v1/cashier/partners/ajpes/87654321", headers=self.headers
            )
        self.assertEqual(response.status_code, 200, response.text)
        suggestion = response.json()["suggestion"]
        self.assertFalse(suggestion["review_confirmed"])
        self.assertEqual(suggestion["verification_source"], "AJPES_REVIEW")
        self.assertEqual(suggestion["tax_number"], "87654321")
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(
                select(func.count()).select_from(partners)
            ).scalar_one(), 0)

    def test_dob_without_attachment_stays_blocked_until_invoice(self):
        from sqlalchemy import select
        from mikrofix_server.models import inventory_items, inventory_lots, purchase_receipts

        supplier_id, article_id = self.seed_supplier_and_article()
        request = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=201, attachments=[]
        )
        response = self.client.post(
            "/v1/sync/operations", json=request, headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result_code"], "ACCEPTED")
        self.assertFalse(response.json()["result"]["stock_released"])
        with self.engine.connect() as connection:
            receipt = connection.execute(select(purchase_receipts)).one()
            lot = connection.execute(select(inventory_lots)).one()
            stock = connection.execute(select(inventory_items).where(
                inventory_items.c.article_uuid == article_id
            )).one()
        self.assertEqual(receipt.status, "PENDING_INVOICE")
        self.assertTrue(receipt.receipt_number.startswith("DOB-2026-"))
        self.assertEqual(lot.status, "PENDING")
        self.assertEqual(str(lot.received_quantity), "2.0000")
        self.assertEqual(str(lot.available_quantity), "0.0000")
        self.assertEqual(str(stock.quantity_on_hand), "0.0000")

    def test_attaching_pra_releases_pending_stock_exactly_once_without_attachment(self):
        from sqlalchemy import func, select
        from mikrofix_server.models import (
            inventory_items, inventory_lots, inventory_price_history,
            purchase_receipt_revisions, purchase_receipts, receipt_number_sequences,
        )

        supplier_id, article_id = self.seed_supplier_and_article()
        receipt_id = uuid4()
        initial = self.purchase_receipt_operation(
            receipt_id, supplier_id, article_id, sequence=211
        )
        created = self.client.post(
            "/v1/sync/operations", json=initial, headers=self.headers
        ).json()
        self.assertEqual(created["result_code"], "ACCEPTED")
        invoice = {
            "operation_id": str(uuid4()),
            "operation_type": "AttachPurchaseInvoice",
            "aggregate_type": "PURCHASE_RECEIPT",
            "aggregate_id": str(receipt_id),
            "expected_version": 1,
            "actor_id": "office-test",
            "device_id": "OFFICE-PC-TEST",
            "device_sequence": 212,
            "occurred_at": "2026-09-12T11:00:00+02:00",
            "payload": {
                "receipt_id": str(receipt_id), "expected_version": 1,
                "invoice_external_no": "INV-SUP-1",
                "invoice_date": "2026-09-12", "invoice_due_date": "2026-10-12",
                "invoice_payment_status": "NEPLACAN", "currency": "EUR",
                "document_total": "24.20", "shipping_total": "2.00",
                "document_review_confirmed": True,
                "lines": initial["payload"]["lines"], "attachments": [],
            },
            "contract_version": 1,
        }
        first = self.client.post(
            "/v1/sync/operations", json=invoice, headers=self.headers
        )
        repeated = self.client.post(
            "/v1/sync/operations", json=invoice, headers=self.headers
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), repeated.json())
        self.assertEqual(first.json()["result_code"], "ACCEPTED")
        self.assertTrue(first.json()["result"]["invoice_receipt_number"].startswith("PRA-2026-"))
        with self.engine.connect() as connection:
            receipt = connection.execute(select(purchase_receipts)).one()
            lot = connection.execute(select(inventory_lots)).one()
            stock = connection.execute(select(inventory_items).where(
                inventory_items.c.article_uuid == article_id
            )).one()
            revision_count = connection.execute(
                select(func.count()).select_from(purchase_receipt_revisions)
            ).scalar_one()
            price_count = connection.execute(
                select(func.count()).select_from(inventory_price_history)
            ).scalar_one()
            pra_sequence = connection.execute(select(
                receipt_number_sequences.c.last_number
            ).where(receipt_number_sequences.c.series_code == "PRA")).scalar_one()
        self.assertEqual(receipt.row_version, 2)
        self.assertEqual(receipt.status, "RELEASED")
        self.assertEqual(str(stock.quantity_on_hand), "2.0000")
        self.assertEqual(str(stock.current_sale_price), "15.00")
        self.assertEqual(lot.status, "AVAILABLE")
        self.assertEqual(str(lot.available_quantity), "2.0000")
        self.assertEqual(revision_count, 2)
        self.assertEqual(price_count, 1)
        self.assertEqual(pra_sequence, 1)

    def test_attached_invoice_cannot_replace_physically_received_quantity(self):
        from sqlalchemy import select
        from mikrofix_server.models import inventory_lots, purchase_receipts

        supplier_id, article_id = self.seed_supplier_and_article()
        receipt_id = uuid4()
        initial = self.purchase_receipt_operation(
            receipt_id, supplier_id, article_id, sequence=215,
            external_no="DOB-IMMUTABLE-PARTS",
        )
        created = self.client.post(
            "/v1/sync/operations", json=initial, headers=self.headers,
        ).json()
        self.assertEqual(created["result_code"], "ACCEPTED")
        changed_lines = deepcopy(initial["payload"]["lines"])
        changed_lines[0].update(quantity="3", supplier_line_total="33.30")
        invoice = {
            "operation_id": str(uuid4()), "operation_type": "AttachPurchaseInvoice",
            "aggregate_type": "PURCHASE_RECEIPT", "aggregate_id": str(receipt_id),
            "expected_version": 1, "actor_id": "office-test",
            "device_id": "OFFICE-PC-TEST", "device_sequence": 216,
            "occurred_at": "2026-09-12T11:00:00+02:00", "contract_version": 1,
            "payload": {
                "receipt_id": str(receipt_id), "expected_version": 1,
                "invoice_external_no": "PRA-IMMUTABLE-PARTS",
                "invoice_date": "2026-09-12", "invoice_due_date": "2026-10-12",
                "invoice_payment_status": "NEPLACAN", "currency": "EUR",
                "document_total": "35.30", "shipping_total": "2.00",
                "document_review_confirmed": True,
                "lines": changed_lines, "attachments": [],
            },
        }
        rejected = self.client.post(
            "/v1/sync/operations", json=invoice, headers=self.headers,
        ).json()
        self.assertEqual(rejected["result_code"], "REJECTED")
        self.assertEqual(rejected["error_code"], "INVALID_RECEIPT_INVOICE")
        self.assertIn("cannot change", rejected["error_message"])
        with self.engine.connect() as connection:
            receipt = connection.execute(select(purchase_receipts)).one()
            lot = connection.execute(select(inventory_lots)).one()
        self.assertEqual(receipt.status, "PENDING_INVOICE")
        self.assertEqual(str(lot.received_quantity), "2.0000")

    def test_two_office_clients_release_one_pending_receipt_only_once(self):
        from sqlalchemy import select
        from mikrofix_server.models import (
            inventory_items, purchase_receipts, receipt_number_sequences,
        )

        supplier_id, article_id = self.seed_supplier_and_article()
        receipt_id = uuid4()
        initial = self.purchase_receipt_operation(
            receipt_id, supplier_id, article_id, sequence=217,
            external_no="DOB-CONCURRENT-PRA",
        )
        created = self.client.post(
            "/v1/sync/operations", json=initial, headers=self.headers,
        ).json()
        self.assertEqual(created["result_code"], "ACCEPTED")

        requests = []
        for sequence, invoice_no in ((218, "PRA-RACE-A"), (219, "PRA-RACE-B")):
            requests.append({
                "operation_id": str(uuid4()),
                "operation_type": "AttachPurchaseInvoice",
                "aggregate_type": "PURCHASE_RECEIPT",
                "aggregate_id": str(receipt_id), "expected_version": 1,
                "actor_id": "office-test", "device_id": "OFFICE-PC-TEST",
                "device_sequence": sequence,
                "occurred_at": "2026-09-12T11:00:00+02:00",
                "contract_version": 1,
                "payload": {
                    "receipt_id": str(receipt_id), "expected_version": 1,
                    "invoice_external_no": invoice_no,
                    "invoice_date": "2026-09-12",
                    "invoice_due_date": "2026-10-12",
                    "invoice_payment_status": "NEPLACAN", "currency": "EUR",
                    "document_total": "24.20", "shipping_total": "2.00",
                    "document_review_confirmed": True,
                    "lines": initial["payload"]["lines"], "attachments": [],
                },
            })

        def submit(request):
            return self.client.post(
                "/v1/sync/operations", json=request, headers=self.headers,
            ).json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, requests))
        self.assertEqual(
            sorted(result["result_code"] for result in results),
            ["ACCEPTED", "REJECTED"],
        )
        rejected = next(
            result for result in results if result["result_code"] == "REJECTED"
        )
        self.assertEqual(rejected["error_code"], "VERSION_CONFLICT")
        with self.engine.connect() as connection:
            receipt = connection.execute(select(purchase_receipts)).one()
            stock = connection.execute(select(inventory_items).where(
                inventory_items.c.article_uuid == article_id
            )).one()
            pra_sequence = connection.execute(select(
                receipt_number_sequences.c.last_number
            ).where(
                receipt_number_sequences.c.series_code == "PRA"
            )).scalar_one()
        self.assertEqual(receipt.row_version, 2)
        self.assertEqual(receipt.status, "RELEASED")
        self.assertEqual(str(stock.quantity_on_hand), "2.0000")
        self.assertEqual(pra_sequence, 1)

    def test_dra_releases_immediately_and_rejects_financial_difference(self):
        from sqlalchemy import func, select
        from mikrofix_server.models import inventory_items, purchase_receipts

        supplier_id, article_id = self.seed_supplier_and_article()
        bad = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=221,
            document_type="DRA", external_no="BAD-TOTAL",
        )
        bad["payload"]["document_total"] = "24.21"
        rejected = self.client.post(
            "/v1/sync/operations", json=bad, headers=self.headers
        ).json()
        self.assertEqual(rejected["result_code"], "REJECTED")
        self.assertEqual(rejected["error_code"], "INVALID_RECEIPT")

        valid = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=222,
            document_type="DRA", external_no="DRA-SUP-1",
        )
        accepted = self.client.post(
            "/v1/sync/operations", json=valid, headers=self.headers
        ).json()
        self.assertEqual(accepted["result_code"], "ACCEPTED")
        self.assertTrue(accepted["result"]["stock_released"])
        self.assertEqual(
            accepted["result"]["inventory_updates"][0]["quantity_available"],
            "2.0000",
        )
        self.assertEqual(
            accepted["result"]["inventory_updates"][0]["identities"][0]
            ["supplier_barcode"],
            "SUP-BARCODE-1",
        )
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(
                select(func.count()).select_from(purchase_receipts)
            ).scalar_one(), 1)
            stock = connection.execute(select(inventory_items).where(
                inventory_items.c.article_uuid == article_id
            )).one()
        self.assertEqual(str(stock.quantity_on_hand), "2.0000")

    def test_service_inventory_bootstrap_is_complete_and_sequence_bound(self):
        supplier_id, article_id = self.seed_supplier_and_article()
        receipt = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=223,
            document_type="DRA", external_no="DRA-BOOTSTRAP-1",
        )
        accepted = self.client.post(
            "/v1/sync/operations", json=receipt, headers=self.headers
        ).json()
        response = self.client.get(
            "/v1/service/inventory-snapshot", headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        snapshot = response.json()
        self.assertIs(snapshot["complete"], True)
        self.assertGreaterEqual(
            snapshot["snapshot_sequence"], accepted["central_sequence"]
        )
        self.assertEqual(len(snapshot["inventory_updates"]), 1)
        item = snapshot["inventory_updates"][0]
        self.assertEqual(item["article_id"], str(article_id))
        self.assertEqual(item["quantity_available"], "2.0000")
        self.assertEqual(
            item["identities"][0]["supplier_barcode"], "SUP-BARCODE-1"
        )

    def test_direct_sale_uses_one_public_line_and_highest_fifo_lot_floor(self):
        from sqlalchemy import func, select
        from mikrofix_server.models import (
            direct_sale_discount_audit, direct_sale_stock_allocations,
            inventory_items, invoices,
        )

        supplier_id, article_id = self.seed_supplier_and_article()
        first = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=224,
            document_type="DRA", external_no="DRA-SALE-1", sale_price="50.00",
        )
        second = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=225,
            document_type="DRA", external_no="DRA-SALE-2", sale_price="50.00",
        )
        second["payload"]["lines"][0].update(
            net_unit_price="20.00", supplier_line_total="44.40"
        )
        second["payload"]["document_total"] = "46.40"
        for operation in (first, second):
            receipt = self.client.post(
                "/v1/sync/operations", json=operation, headers=self.headers
            ).json()
            self.assertEqual(receipt["result_code"], "ACCEPTED", receipt)

        sale_id = uuid4()
        issue = self.direct_sale_operation(
            sale_id, sequence=226, quantity="3", discount="100",
            amount="73.08",
        )
        quote_response = self.client.post(
            "/v1/cashier/direct-sale-quote",
            json={"lines": issue["payload"]["lines"]}, headers=self.headers,
        )
        self.assertEqual(quote_response.status_code, 200, quote_response.text)
        quote = quote_response.json()
        self.assertEqual(quote["total_amount"], "73.08")
        self.assertEqual(quote["lines"][0]["unit_price"], "24.36")
        self.assertNotIn("requested_discount_percent", quote["lines"][0])
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(
                select(func.count()).select_from(invoices)
            ).scalar_one(), 0)
        accepted = self.client.post(
            "/v1/sync/operations", json=issue, headers=self.headers
        ).json()
        repeated = self.client.post(
            "/v1/sync/operations", json=issue, headers=self.headers
        ).json()
        duplicate_sale = deepcopy(issue)
        duplicate_sale["operation_id"] = str(uuid4())
        duplicate_sale["device_sequence"] = 229
        duplicate = self.client.post(
            "/v1/sync/operations", json=duplicate_sale, headers=self.headers
        ).json()
        self.assertEqual(accepted, repeated)
        self.assertEqual(accepted["result_code"], "ACCEPTED")
        self.assertEqual(duplicate["result_code"], "REJECTED")
        self.assertEqual(duplicate["error_code"], "DIRECT_SALE_ALREADY_EXISTS")
        self.assertEqual(accepted["result"]["total_amount"], "73.08")
        self.assertEqual(len(accepted["result"]["lines"]), 1)
        line = accepted["result"]["lines"][0]
        self.assertEqual(line["unit_price"], "24.36")
        self.assertEqual(line["applied_discount_percent"], "51.2800")
        self.assertNotIn("allocations", line)
        self.assertNotIn("requested_discount_percent", line)
        with self.engine.connect() as connection:
            invoice = connection.execute(select(invoices)).one()
            stock = connection.execute(select(inventory_items).where(
                inventory_items.c.article_uuid == article_id
            )).one()
            allocation_count, allocated = connection.execute(select(
                func.count(), func.sum(direct_sale_stock_allocations.c.quantity)
            )).one()
            discount_audit = connection.execute(
                select(direct_sale_discount_audit)
            ).one()
        self.assertEqual(invoice.source_type, "DIRECT_SALE")
        self.assertEqual(invoice.direct_sale_uuid, sale_id)
        self.assertIsNone(invoice.service_order_uuid)
        self.assertEqual(invoice.billing_customer_json["source"], "WALK_IN")
        self.assertNotIn(
            "requested_discount_percent", invoice.snapshot_json["lines"][0]
        )
        self.assertEqual(str(stock.quantity_on_hand), "1.0000")
        self.assertEqual(allocation_count, 2)
        self.assertEqual(str(allocated), "3.0000")
        self.assertEqual(str(discount_audit.requested_discount_percent), "100.0000")
        self.assertEqual(str(discount_audit.applied_discount_percent), "51.2800")
        self.assertEqual(discount_audit.decision, "CLAMPED_TO_MINIMUM")

        invoice_response = self.client.get(
            f"/v1/cashier/invoices/{invoice.invoice_uuid}", headers=self.headers
        )
        self.assertEqual(invoice_response.status_code, 200)
        recovered = invoice_response.json()
        self.assertEqual(recovered["invoice_number"], invoice.invoice_number)
        self.assertEqual(recovered["source_type"], "DIRECT_SALE")
        self.assertNotIn(
            "requested_discount_percent", recovered["snapshot"]["lines"][0]
        )

        inventory_response = self.client.get(
            "/v1/cashier/inventory", params={"q": "SUP-BARCODE-1"},
            headers=self.headers,
        )
        self.assertEqual(inventory_response.status_code, 200)
        cashier_item = inventory_response.json()["inventory"][0]
        self.assertEqual(cashier_item["article_quantity_available"], "1.0000")
        self.assertEqual(cashier_item["track_mode"], "QUANTITY")
        self.assertEqual(cashier_item["available_lot_serials"], [])
        self.assertNotIn("landed_unit_cost", cashier_item)

    def test_service_and_cashier_cannot_consume_the_same_last_central_unit(self):
        from sqlalchemy import func, select
        from mikrofix_server.models import (
            inventory_items, invoice_drafts, invoices,
            service_part_consumptions,
        )

        supplier_id, article_id = self.seed_supplier_and_article()
        receipt = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=230,
            document_type="DRA", external_no="DRA-SERVICE-CASHIER-RACE",
            quantity="1",
        )
        receipt["payload"]["lines"][0]["supplier_line_total"] = "11.10"
        receipt["payload"]["document_total"] = "13.10"
        posted = self.client.post(
            "/v1/sync/operations", json=receipt, headers=self.headers
        ).json()
        self.assertEqual(posted["result_code"], "ACCEPTED", posted)

        order = self.operation(sequence=231)
        self.assertEqual(self.client.post(
            "/v1/sync/operations", json=order, headers=self.headers
        ).json()["result_code"], "ACCEPTED")
        completion = self.completion_operation(
            order, sequence=232, barcode="SUP-BARCODE-1", article_id=article_id,
        )
        completion["payload"]["parts"][0]["lot_serial"] = ""
        sale = self.direct_sale_operation(uuid4(), sequence=233, amount="15.00")

        def submit(payload):
            return self.client.post(
                "/v1/sync/operations", json=payload, headers=self.headers
            ).json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, (completion, sale)))
        self.assertEqual(
            sorted(result["result_code"] for result in results),
            ["ACCEPTED", "REJECTED"],
        )
        with self.engine.connect() as connection:
            stock = connection.execute(select(inventory_items).where(
                inventory_items.c.article_uuid == article_id
            )).one()
            direct_count = connection.execute(
                select(func.count()).select_from(invoices)
            ).scalar_one()
            service_count = connection.execute(
                select(func.count()).select_from(service_part_consumptions)
            ).scalar_one()
            draft_count = connection.execute(
                select(func.count()).select_from(invoice_drafts)
            ).scalar_one()
        self.assertEqual(str(stock.quantity_on_hand), "0.0000")
        self.assertEqual(direct_count + service_count, 1)
        self.assertEqual(draft_count, service_count)

    def test_two_cashiers_same_direct_sale_get_one_invoice_and_one_number(self):
        from sqlalchemy import func, select
        from mikrofix_server.models import (
            inventory_items, invoice_number_sequences, invoices,
        )

        supplier_id, article_id = self.seed_supplier_and_article()
        receipt = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=234,
            document_type="DRA", external_no="DRA-DIRECT-SALE-RACE",
        )
        self.assertEqual(self.client.post(
            "/v1/sync/operations", json=receipt, headers=self.headers
        ).json()["result_code"], "ACCEPTED")
        sale_id = uuid4()
        first = self.direct_sale_operation(sale_id, sequence=235)
        second = self.direct_sale_operation(sale_id, sequence=236)
        second["actor_id"] = "blagajnik-b"
        second["device_id"] = "POS-PC-B"

        def submit(payload):
            return self.client.post(
                "/v1/sync/operations", json=payload, headers=self.headers
            ).json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, (first, second)))
        self.assertEqual(
            sorted(result["result_code"] for result in results),
            ["ACCEPTED", "REJECTED"],
        )
        rejected = next(
            result for result in results if result["result_code"] == "REJECTED"
        )
        self.assertEqual(rejected["error_code"], "DIRECT_SALE_ALREADY_EXISTS")
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(
                select(func.count()).select_from(invoices)
            ).scalar_one(), 1)
            self.assertEqual(connection.execute(
                select(invoice_number_sequences.c.last_number)
            ).scalar_one(), 1)
            stock_quantity = connection.execute(
                select(inventory_items.c.quantity_on_hand).where(
                    inventory_items.c.article_uuid == article_id
                )
            ).scalar_one()
        self.assertEqual(str(stock_quantity), "1.0000")

    def test_storno_creates_one_negative_document_without_restoring_stock(self):
        from sqlalchemy import func, select
        from mikrofix_server.models import (
            direct_sale_stock_allocations, inventory_items,
            invoice_number_sequences, invoices,
        )

        supplier_id, article_id = self.seed_supplier_and_article()
        receipt = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=310,
            document_type="DRA", external_no="DRA-STORNO-NO-STOCK",
        )
        self.assertEqual(self.client.post(
            "/v1/sync/operations", json=receipt, headers=self.headers
        ).json()["result_code"], "ACCEPTED")
        issued = self.client.post(
            "/v1/sync/operations",
            json=self.direct_sale_operation(uuid4(), sequence=311),
            headers=self.headers,
        ).json()
        self.assertEqual(issued["result_code"], "ACCEPTED", issued)
        invoice_id = issued["result"]["invoice_id"]
        storno = self.storno_invoice_operation(invoice_id, sequence=312)
        first = self.client.post(
            "/v1/sync/operations", json=storno, headers=self.headers
        ).json()
        repeated = self.client.post(
            "/v1/sync/operations", json=storno, headers=self.headers
        ).json()
        duplicate = deepcopy(storno)
        duplicate["operation_id"] = str(uuid4())
        duplicate["device_sequence"] = 313
        rejected = self.client.post(
            "/v1/sync/operations", json=duplicate, headers=self.headers
        ).json()

        self.assertEqual(first, repeated)
        self.assertEqual(first["result_code"], "ACCEPTED", first)
        self.assertEqual(first["result"]["stock_effect"], "NONE")
        self.assertEqual(first["result"]["service_effect"], "NONE")
        self.assertEqual(rejected["error_code"], "INVOICE_ALREADY_REVERSED")
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(invoices).order_by(invoices.c.fiscal_sequence)
            ).all()
            stock = connection.execute(select(inventory_items).where(
                inventory_items.c.article_uuid == article_id
            )).one()
            allocation_count = connection.execute(
                select(func.count()).select_from(direct_sale_stock_allocations)
            ).scalar_one()
            last_number = connection.execute(
                select(invoice_number_sequences.c.last_number)
            ).scalar_one()
        self.assertEqual(len(rows), 2)
        original, reversal = rows
        self.assertEqual(original.status, "REVERSED")
        self.assertEqual(original.row_version, 2)
        self.assertEqual(reversal.source_type, "REVERSAL")
        self.assertEqual(reversal.reversal_of_invoice_uuid, original.invoice_uuid)
        self.assertEqual(reversal.status, "ISSUED")
        self.assertEqual(str(reversal.total_amount), "-15.00")
        self.assertEqual(reversal.snapshot_json["stock_effect"], "NONE")
        self.assertEqual(str(stock.quantity_on_hand), "1.0000")
        self.assertEqual(allocation_count, 1)
        self.assertEqual(last_number, 2)

    def test_two_cashiers_cannot_storno_the_same_invoice_concurrently(self):
        from sqlalchemy import func, select
        from mikrofix_server.models import invoice_number_sequences, invoices

        supplier_id, article_id = self.seed_supplier_and_article()
        receipt = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=324,
            document_type="DRA", external_no="DRA-STORNO-RACE",
        )
        self.assertEqual(self.client.post(
            "/v1/sync/operations", json=receipt, headers=self.headers
        ).json()["result_code"], "ACCEPTED")
        issued = self.client.post(
            "/v1/sync/operations",
            json=self.direct_sale_operation(uuid4(), sequence=325),
            headers=self.headers,
        ).json()
        self.assertEqual(issued["result_code"], "ACCEPTED", issued)
        invoice_id = issued["result"]["invoice_id"]

        first = self.storno_invoice_operation(invoice_id, sequence=326)
        second = self.storno_invoice_operation(invoice_id, sequence=327)
        second["actor_id"] = "blagajnik-b"
        second["device_id"] = "POS-PC-B"

        def submit(payload):
            return self.client.post(
                "/v1/sync/operations", json=payload, headers=self.headers
            ).json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, (first, second)))

        self.assertEqual(
            sorted(result["result_code"] for result in results),
            ["ACCEPTED", "REJECTED"],
        )
        rejected = next(
            result for result in results if result["result_code"] == "REJECTED"
        )
        self.assertIn(
            rejected["error_code"],
            {"INVOICE_ALREADY_REVERSED", "VERSION_CONFLICT"},
        )
        with self.engine.connect() as connection:
            reversal_count = connection.execute(
                select(func.count()).select_from(invoices).where(
                    invoices.c.source_type == "REVERSAL"
                )
            ).scalar_one()
            last_number = connection.execute(
                select(invoice_number_sequences.c.last_number)
            ).scalar_one()
        self.assertEqual(reversal_count, 1)
        self.assertEqual(last_number, 2)

    def test_cash_storno_waits_for_furs_and_books_one_cash_refund(self):
        from sqlalchemy import func, select
        from mikrofix_server.fiscal_worker import process_fiscal_invoice
        from mikrofix_server.models import (
            cash_journal_entries, inventory_items, invoices,
        )

        class ConfirmingProvider:
            def __init__(self):
                self.requests = []
                self.submissions = 0

            def prepare_request(self, request, *, message_id):
                self.requests.append(dict(request))
                return {"InvoiceRequest": {
                    "Header": {"MessageID": message_id},
                    "Invoice": {"ProtectedID": "d" * 32},
                }}

            def submit_prepared(self, _payload):
                self.submissions += 1
                return {
                    "status": "CONFIRMED", "zoi": "d" * 32,
                    "eor": f"EOR-STORNO-{self.submissions}",
                    "response": {"ok": True},
                }

        supplier_id, article_id = self.seed_supplier_and_article()
        receipt = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=314,
            document_type="DRA", external_no="DRA-CASH-STORNO",
        )
        self.assertEqual(self.client.post(
            "/v1/sync/operations", json=receipt, headers=self.headers
        ).json()["result_code"], "ACCEPTED")
        self.assertEqual(self.client.post(
            "/v1/sync/operations", json=self.open_cash_operation(sequence=315),
            headers=self.headers,
        ).json()["result_code"], "ACCEPTED")
        issued = self.client.post(
            "/v1/sync/operations",
            json=self.direct_sale_operation(
                uuid4(), sequence=316, method="CASH", confirmed=True,
            ), headers=self.headers,
        ).json()
        original_id = issued["result"]["invoice_id"]
        original_provider = ConfirmingProvider()
        process_fiscal_invoice(self.engine, original_id, original_provider)

        storno = self.client.post(
            "/v1/sync/operations",
            json=self.storno_invoice_operation(
                original_id, sequence=317, expected_version=2,
            ), headers=self.headers,
        ).json()
        self.assertEqual(storno["result_code"], "ACCEPTED", storno)
        self.assertEqual(storno["result"]["invoice_status"], "REVERSAL_PENDING")
        reversal_id = storno["result"]["reversal_invoice_id"]
        reversal_provider = ConfirmingProvider()
        confirmed = process_fiscal_invoice(
            self.engine, reversal_id, reversal_provider
        )
        repeated = process_fiscal_invoice(
            self.engine, reversal_id, reversal_provider
        )
        self.assertEqual(confirmed, repeated)
        self.assertEqual(reversal_provider.submissions, 1)
        self.assertEqual(
            reversal_provider.requests[0]["reference_invoice"]
            ["ReferenceInvoiceIdentifier"]["InvoiceNumber"],
            "1",
        )
        with self.engine.connect() as connection:
            original = connection.execute(select(invoices).where(
                invoices.c.invoice_uuid == original_id
            )).one()
            reversal = connection.execute(select(invoices).where(
                invoices.c.invoice_uuid == reversal_id
            )).one()
            entries = connection.execute(
                select(cash_journal_entries).order_by(
                    cash_journal_entries.c.entry_no
                )
            ).all()
            stock = connection.execute(select(inventory_items).where(
                inventory_items.c.article_uuid == article_id
            )).one()
        self.assertEqual(original.status, "REVERSED")
        self.assertEqual(original.row_version, 4)
        self.assertEqual(reversal.status, "ISSUED")
        self.assertEqual([str(row.amount) for row in entries], ["15.00", "-15.00"])
        self.assertEqual(entries[1].entry_type, "CASH_REFUND")
        self.assertEqual(str(stock.quantity_on_hand), "1.0000")

    def test_uncertain_storno_furs_retry_reuses_the_same_document_and_request(self):
        from sqlalchemy import func, select
        from mikrofix_server.fiscal_worker import process_fiscal_invoice
        from mikrofix_server.models import fiscal_outbox, invoices

        class UncertainThenConfirmingProvider:
            def __init__(self):
                self.prepared_inputs = []
                self.submitted_payloads = []

            def prepare_request(self, request, *, message_id):
                self.prepared_inputs.append(dict(request))
                return {"InvoiceRequest": {
                    "Header": {"MessageID": message_id},
                    "Invoice": {"ProtectedID": "e" * 32},
                }}

            def submit_prepared(self, payload):
                self.submitted_payloads.append(deepcopy(payload))
                if len(self.submitted_payloads) == 1:
                    raise TimeoutError("FURS result unknown")
                return {
                    "status": "CONFIRMED", "zoi": "e" * 32,
                    "eor": "EOR-STORNO-RETRY-1", "response": {"ok": True},
                }

        supplier_id, article_id = self.seed_supplier_and_article()
        receipt = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=328,
            document_type="DRA", external_no="DRA-STORNO-UNCERTAIN",
        )
        self.assertEqual(self.client.post(
            "/v1/sync/operations", json=receipt, headers=self.headers
        ).json()["result_code"], "ACCEPTED")
        self.assertEqual(self.client.post(
            "/v1/sync/operations", json=self.open_cash_operation(sequence=329),
            headers=self.headers,
        ).json()["result_code"], "ACCEPTED")
        issued = self.client.post(
            "/v1/sync/operations",
            json=self.direct_sale_operation(
                uuid4(), sequence=330, method="CASH", confirmed=True,
            ), headers=self.headers,
        ).json()
        original_id = issued["result"]["invoice_id"]

        class OriginalConfirmingProvider:
            def prepare_request(self, _request, *, message_id):
                return {"InvoiceRequest": {
                    "Header": {"MessageID": message_id},
                    "Invoice": {"ProtectedID": "f" * 32},
                }}

            def submit_prepared(self, _payload):
                return {
                    "status": "CONFIRMED", "zoi": "f" * 32,
                    "eor": "EOR-ORIGINAL-1", "response": {"ok": True},
                }

        process_fiscal_invoice(
            self.engine, original_id, OriginalConfirmingProvider()
        )
        storno = self.client.post(
            "/v1/sync/operations",
            json=self.storno_invoice_operation(
                original_id, sequence=331, expected_version=2,
            ), headers=self.headers,
        ).json()
        self.assertEqual(storno["result_code"], "ACCEPTED", storno)
        reversal_id = storno["result"]["reversal_invoice_id"]
        provider = UncertainThenConfirmingProvider()

        uncertain = process_fiscal_invoice(self.engine, reversal_id, provider)
        self.assertEqual(uncertain["status"], "FISCAL_RECONCILIATION")
        with self.engine.connect() as connection:
            original = connection.execute(select(invoices).where(
                invoices.c.invoice_uuid == original_id
            )).one()
            reversal = connection.execute(select(invoices).where(
                invoices.c.invoice_uuid == reversal_id
            )).one()
            outbox = connection.execute(select(fiscal_outbox).where(
                fiscal_outbox.c.invoice_uuid == reversal_id
            )).one()
        self.assertEqual(original.status, "REVERSAL_PENDING")
        self.assertEqual(reversal.status, "FISCAL_RECONCILIATION")
        self.assertEqual(outbox.status, "UNCERTAIN")

        confirmed = process_fiscal_invoice(self.engine, reversal_id, provider)
        self.assertEqual(confirmed["status"], "ISSUED")
        self.assertEqual(len(provider.prepared_inputs), 1)
        self.assertEqual(len(provider.submitted_payloads), 2)
        self.assertEqual(
            provider.submitted_payloads[0], provider.submitted_payloads[1]
        )
        with self.engine.connect() as connection:
            rows = connection.execute(select(invoices)).all()
            original = next(row for row in rows if str(row.invoice_uuid) == original_id)
            reversal = next(row for row in rows if str(row.invoice_uuid) == reversal_id)
            reversal_count = connection.execute(
                select(func.count()).select_from(invoices).where(
                    invoices.c.source_type == "REVERSAL"
                )
            ).scalar_one()
        self.assertEqual(original.status, "REVERSED")
        self.assertEqual(reversal.status, "ISSUED")
        self.assertEqual(reversal_count, 1)

    def test_sale_return_enters_quarantine_and_office_release_restores_exact_lot(self):
        from sqlalchemy import select
        from mikrofix_server.models import (
            direct_sale_stock_allocations, inventory_items, inventory_lots,
            physical_stock_returns,
        )

        supplier_id, article_id = self.seed_supplier_and_article()
        receipt = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=332,
            document_type="DRA", external_no="DRA-PHYSICAL-RETURN",
        )
        self.assertEqual(self.client.post(
            "/v1/sync/operations", json=receipt, headers=self.headers
        ).json()["result_code"], "ACCEPTED")
        issued = self.client.post(
            "/v1/sync/operations",
            json=self.direct_sale_operation(uuid4(), sequence=333),
            headers=self.headers,
        ).json()
        invoice_id = issued["result"]["invoice_id"]
        with self.engine.connect() as connection:
            allocation = connection.execute(select(
                direct_sale_stock_allocations
            )).one()
        returnable = self.client.get(
            f"/v1/cashier/invoices/{invoice_id}/returnable-stock",
            headers=self.headers,
        )
        self.assertEqual(returnable.status_code, 200, returnable.text)
        self.assertEqual(len(returnable.json()["returnable_stock"]), 1)
        self.assertEqual(
            returnable.json()["returnable_stock"][0]["invoice_line_id"],
            str(allocation.sale_line_uuid),
        )
        self.assertEqual(
            returnable.json()["returnable_stock"][0]["lot_id"],
            str(allocation.lot_uuid),
        )
        self.assertEqual(
            returnable.json()["returnable_stock"][0]["returnable_quantity"],
            "1.0000",
        )
        return_id = uuid4()
        received_operation = self.receive_stock_return_operation(
            return_id, invoice_id, allocation.sale_line_uuid,
            allocation.lot_uuid, sequence=334,
        )
        received = self.client.post(
            "/v1/sync/operations", json=received_operation, headers=self.headers
        ).json()
        repeated = self.client.post(
            "/v1/sync/operations", json=received_operation, headers=self.headers
        ).json()
        self.assertEqual(received, repeated)
        self.assertEqual(received["result_code"], "ACCEPTED", received)
        self.assertEqual(received["result"]["status"], "QUARANTINED")
        self.assertEqual(received["result"]["stock_effect"], "NONE")
        exhausted = self.client.get(
            f"/v1/cashier/invoices/{invoice_id}/returnable-stock",
            headers=self.headers,
        ).json()
        self.assertEqual(exhausted["returnable_stock"], [])
        quarantine = self.client.get(
            "/v1/office/physical-stock-returns",
            headers=self.headers,
        )
        self.assertEqual(quarantine.status_code, 200, quarantine.text)
        self.assertEqual(len(quarantine.json()["returns"]), 1)
        self.assertEqual(
            quarantine.json()["returns"][0]["return_id"], str(return_id)
        )
        self.assertEqual(
            quarantine.json()["returns"][0]["status"], "QUARANTINED"
        )
        with self.engine.connect() as connection:
            returned = connection.execute(select(physical_stock_returns)).one()
            lot_before = connection.execute(select(inventory_lots).where(
                inventory_lots.c.lot_uuid == allocation.lot_uuid
            )).one()
            item_before = connection.execute(select(inventory_items).where(
                inventory_items.c.article_uuid == article_id
            )).one()
        self.assertEqual(returned.status, "QUARANTINED")
        self.assertEqual(str(lot_before.available_quantity), "1.0000")
        self.assertEqual(str(item_before.quantity_on_hand), "1.0000")

        resolved_operation = self.resolve_stock_return_operation(
            return_id, sequence=335
        )
        resolved = self.client.post(
            "/v1/sync/operations", json=resolved_operation, headers=self.headers
        ).json()
        resolved_repeated = self.client.post(
            "/v1/sync/operations", json=resolved_operation, headers=self.headers
        ).json()
        self.assertEqual(resolved, resolved_repeated)
        self.assertEqual(resolved["result"]["status"], "RELEASED_TO_STOCK")
        self.assertEqual(resolved["result"]["stock_effect"], "INCREASED")
        self.assertEqual(self.client.get(
            "/v1/office/physical-stock-returns",
            headers=self.headers,
        ).json()["returns"], [])
        released = self.client.get(
            "/v1/office/physical-stock-returns",
            params={"return_status": "RELEASED_TO_STOCK"},
            headers=self.headers,
        ).json()["returns"]
        self.assertEqual(len(released), 1)
        self.assertEqual(released[0]["return_id"], str(return_id))
        with self.engine.connect() as connection:
            returned = connection.execute(select(physical_stock_returns)).one()
            lot_after = connection.execute(select(inventory_lots).where(
                inventory_lots.c.lot_uuid == allocation.lot_uuid
            )).one()
            item_after = connection.execute(select(inventory_items).where(
                inventory_items.c.article_uuid == article_id
            )).one()
        self.assertEqual(returned.row_version, 2)
        self.assertEqual(returned.status, "RELEASED_TO_STOCK")
        self.assertEqual(str(lot_after.available_quantity), "2.0000")
        self.assertEqual(str(item_after.quantity_on_hand), "2.0000")

    def test_concurrent_receipts_cannot_return_more_than_the_sold_lot_quantity(self):
        from sqlalchemy import func, select
        from mikrofix_server.models import (
            direct_sale_stock_allocations, inventory_items,
            physical_stock_returns,
        )

        supplier_id, article_id = self.seed_supplier_and_article()
        receipt = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=336,
            document_type="DRA", external_no="DRA-PHYSICAL-RETURN-RACE",
        )
        self.client.post(
            "/v1/sync/operations", json=receipt, headers=self.headers
        )
        issued = self.client.post(
            "/v1/sync/operations",
            json=self.direct_sale_operation(uuid4(), sequence=337),
            headers=self.headers,
        ).json()
        invoice_id = issued["result"]["invoice_id"]
        with self.engine.connect() as connection:
            allocation = connection.execute(select(
                direct_sale_stock_allocations
            )).one()
        first = self.receive_stock_return_operation(
            uuid4(), invoice_id, allocation.sale_line_uuid,
            allocation.lot_uuid, sequence=338,
        )
        second = self.receive_stock_return_operation(
            uuid4(), invoice_id, allocation.sale_line_uuid,
            allocation.lot_uuid, sequence=339,
        )
        second["actor_id"] = "blagajnik-b"
        second["device_id"] = "POS-PC-B"

        def submit(payload):
            return self.client.post(
                "/v1/sync/operations", json=payload, headers=self.headers
            ).json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, (first, second)))
        self.assertEqual(
            sorted(result["result_code"] for result in results),
            ["ACCEPTED", "REJECTED"],
        )
        rejected = next(
            result for result in results if result["result_code"] == "REJECTED"
        )
        self.assertEqual(
            rejected["error_code"], "PHYSICAL_RETURN_EXCEEDS_SOLD_QUANTITY"
        )
        with self.engine.connect() as connection:
            count = connection.execute(
                select(func.count()).select_from(physical_stock_returns)
            ).scalar_one()
            item = connection.execute(select(inventory_items).where(
                inventory_items.c.article_uuid == article_id
            )).one()
        self.assertEqual(count, 1)
        self.assertEqual(str(item.quantity_on_hand), "1.0000")

    def test_removed_service_part_can_be_quarantined_without_reopening_service(self):
        from sqlalchemy import select
        from mikrofix_server.models import (
            inventory_items, physical_stock_returns, service_orders,
            service_stock_allocations,
        )

        supplier_id, article_id = self.seed_supplier_and_article()
        receipt = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=340,
            document_type="DRA", external_no="DRA-SERVICE-REMOVAL",
        )
        self.assertEqual(self.client.post(
            "/v1/sync/operations", json=receipt, headers=self.headers
        ).json()["result_code"], "ACCEPTED")
        order = self.operation(sequence=341)
        self.assertEqual(self.client.post(
            "/v1/sync/operations", json=order, headers=self.headers
        ).json()["result_code"], "ACCEPTED")
        completion = self.completion_operation(
            order, sequence=342, barcode="SUP-BARCODE-1",
            article_id=article_id,
        )
        completion["payload"]["parts"][0]["lot_serial"] = ""
        completed = self.client.post(
            "/v1/sync/operations", json=completion, headers=self.headers
        ).json()
        self.assertEqual(completed["result_code"], "ACCEPTED", completed)
        draft_id = completed["result"]["draft_uuid"]
        issued = self.client.post(
            "/v1/sync/operations",
            json=self.issue_operation(draft_id, sequence=343),
            headers=self.headers,
        ).json()
        invoice_id = issued["result"]["invoice_id"]
        with self.engine.connect() as connection:
            allocation = connection.execute(select(
                service_stock_allocations
            )).one()
        page = self.client.get(
            "/v1/sync/changes?after=0", headers=self.headers
        ).json()
        billing_event = next(
            change for change in page["changes"]
            if change["event_type"] == "ServiceBillingCompleted"
            and change["aggregate_id"] == order["aggregate_id"]
        )
        returnable = billing_event["payload"]["returnable_parts"]
        self.assertEqual(len(returnable), 1)
        self.assertEqual(returnable[0]["invoice_id"], invoice_id)
        self.assertEqual(
            returnable[0]["invoice_line_id"], str(allocation.service_line_uuid)
        )
        self.assertEqual(returnable[0]["lot_id"], str(allocation.lot_uuid))
        self.assertEqual(returnable[0]["removable_quantity"], "1.0000")
        snapshot = self.client.get(
            "/v1/service/returnable-parts-snapshot", headers=self.headers
        ).json()
        self.assertIs(snapshot["complete"], True)
        matching_snapshot_rows = [
            item for item in snapshot["returnable_parts"]
            if item["invoice_id"] == invoice_id
        ]
        self.assertEqual(matching_snapshot_rows, returnable)
        return_id = uuid4()
        operation = self.receive_stock_return_operation(
            return_id, invoice_id, allocation.service_line_uuid,
            allocation.lot_uuid, sequence=344,
            operation_type="ReceiveRemovedServicePart",
        )
        operation["actor_id"] = "serviser-test"
        operation["device_id"] = "SERVICE-PC-TEST"
        response = self.client.post(
            "/v1/sync/operations", json=operation, headers=self.headers
        ).json()
        self.assertEqual(response["result_code"], "ACCEPTED", response)
        self.assertEqual(
            response["result"]["service_order_id"], order["aggregate_id"]
        )
        referred = self.client.post(
            "/v1/sync/operations",
            json=self.resolve_stock_return_operation(
                return_id, sequence=345, decision="REFER_TO_CLAIM",
            ), headers=self.headers,
        ).json()
        self.assertEqual(referred["result"]["status"], "CLAIM_REVIEW_REQUIRED")
        self.assertEqual(referred["result"]["stock_effect"], "NONE")
        with self.engine.connect() as connection:
            returned = connection.execute(select(physical_stock_returns)).one()
            service = connection.execute(select(service_orders).where(
                service_orders.c.sync_uuid == order["aggregate_id"]
            )).one()
            stock = connection.execute(select(inventory_items).where(
                inventory_items.c.article_uuid == article_id
            )).one()
        self.assertEqual(returned.receipt_source, "SERVICE_REMOVAL")
        self.assertEqual(returned.status, "CLAIM_REVIEW_REQUIRED")
        self.assertEqual(service.payload_json["technical_status"], "BILLING_COMPLETED")
        self.assertEqual(str(stock.quantity_on_hand), "1.0000")

    def test_service_invoice_storno_does_not_reopen_service_or_restore_part(self):
        from sqlalchemy import func, select
        from mikrofix_server.models import (
            inventory_items, invoice_drafts, invoices, service_orders,
            service_part_consumptions,
        )

        order, draft_id = self.prepared_draft(
            sequence=318, barcode="SERVICE-STORNO-PART"
        )
        issued = self.client.post(
            "/v1/sync/operations",
            json=self.issue_operation(draft_id, sequence=320),
            headers=self.headers,
        ).json()
        invoice_id = issued["result"]["invoice_id"]
        storno = self.client.post(
            "/v1/sync/operations",
            json=self.storno_invoice_operation(invoice_id, sequence=321),
            headers=self.headers,
        ).json()
        self.assertEqual(storno["result_code"], "ACCEPTED", storno)
        with self.engine.connect() as connection:
            service = connection.execute(select(service_orders).where(
                service_orders.c.sync_uuid == order["aggregate_id"]
            )).one()
            draft = connection.execute(select(invoice_drafts).where(
                invoice_drafts.c.draft_uuid == draft_id
            )).one()
            original = connection.execute(select(invoices).where(
                invoices.c.invoice_uuid == invoice_id
            )).one()
            stock = connection.execute(select(inventory_items).where(
                inventory_items.c.barcode == "SERVICE-STORNO-PART"
            )).one()
            consumption_count = connection.execute(
                select(func.count()).select_from(service_part_consumptions)
            ).scalar_one()
        self.assertEqual(service.payload_json["technical_status"], "BILLING_COMPLETED")
        self.assertEqual(draft.status, "INVOICED")
        self.assertEqual(original.status, "REVERSED")
        self.assertEqual(str(stock.quantity_on_hand), "0.0000")
        self.assertEqual(consumption_count, 1)

    def test_cashier_without_storno_permission_is_rejected_before_write(self):
        from sqlalchemy import func, select
        from mikrofix_server.models import invoices

        supplier_id, article_id = self.seed_supplier_and_article()
        receipt = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=322,
            document_type="DRA", external_no="DRA-STORNO-PERMISSION",
        )
        self.client.post(
            "/v1/sync/operations", json=receipt, headers=self.headers
        )
        issued = self.client.post(
            "/v1/sync/operations",
            json=self.direct_sale_operation(uuid4(), sequence=323),
            headers=self.headers,
        ).json()
        invoice_id = issued["result"]["invoice_id"]
        login = self.central_login(role="CASHIER", application="CASHIER")
        self.assertEqual(login.status_code, 200, login.text)
        response = self.client.post(
            "/v1/sync/operations",
            json=self.storno_invoice_operation(invoice_id, sequence=1),
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        with self.engine.connect() as connection:
            reversal_count = connection.execute(
                select(func.count()).select_from(invoices).where(
                    invoices.c.source_type == "REVERSAL"
                )
            ).scalar_one()
        self.assertEqual(reversal_count, 0)

    def test_direct_serial_sale_requires_and_consumes_the_exact_serial(self):
        from sqlalchemy import select
        from mikrofix_server.models import inventory_lots

        supplier_id, article_id = self.seed_supplier_and_article(track_mode="SERIAL")
        receipt = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=237,
            document_type="DRA", external_no="DRA-SERIAL-SALE", quantity="1",
        )
        receipt["payload"]["lines"][0].update(
            supplier_line_total="11.10", lot_serial="SERIAL-0001"
        )
        receipt["payload"]["document_total"] = "13.10"
        self.assertEqual(self.client.post(
            "/v1/sync/operations", json=receipt, headers=self.headers
        ).json()["result_code"], "ACCEPTED")

        inventory = self.client.get(
            "/v1/cashier/inventory", params={"q": "SUP-BARCODE-1"},
            headers=self.headers,
        ).json()["inventory"][0]
        self.assertEqual(inventory["track_mode"], "SERIAL")
        self.assertEqual(inventory["available_lot_serials"], ["SERIAL-0001"])

        missing = self.direct_sale_operation(uuid4(), sequence=238)
        missing_result = self.client.post(
            "/v1/sync/operations", json=missing, headers=self.headers
        ).json()
        self.assertEqual(missing_result["error_code"], "SERIAL_IDENTITY_REQUIRED")
        wrong = self.direct_sale_operation(
            uuid4(), sequence=239, lot_serial="SERIAL-OTHER"
        )
        wrong_result = self.client.post(
            "/v1/sync/operations", json=wrong, headers=self.headers
        ).json()
        self.assertEqual(wrong_result["error_code"], "MISSING_LOT_COST")
        exact = self.direct_sale_operation(
            uuid4(), sequence=240, lot_serial="SERIAL-0001"
        )
        exact_result = self.client.post(
            "/v1/sync/operations", json=exact, headers=self.headers
        ).json()
        self.assertEqual(exact_result["result_code"], "ACCEPTED")
        with self.engine.connect() as connection:
            lot = connection.execute(select(inventory_lots)).one()
        self.assertEqual(str(lot.available_quantity), "0.0000")
        self.assertEqual(lot.status, "CONSUMED")

    def test_direct_t1_labor_uses_the_versioned_central_tariff_and_floor(self):
        from sqlalchemy import func, select
        from mikrofix_server.models import (
            direct_sale_discount_audit, invoices, service_tariffs,
        )

        issue = self.direct_sale_operation(
            uuid4(), sequence=241, amount="22.00", discount="100",
        )
        issue["payload"]["lines"][0] = {
            "line_id": str(uuid4()), "line_kind": "SERVICE",
            "barcode": "", "quantity": "1", "service_tier": "T1",
            "elapsed_minutes": 75, "requested_discount_percent": "100",
            "lot_serial": "",
        }
        accepted = self.client.post(
            "/v1/sync/operations", json=issue, headers=self.headers
        ).json()
        self.assertEqual(accepted["result_code"], "ACCEPTED", accepted)
        self.assertEqual(accepted["result"]["total_amount"], "22.00")
        line = accepted["result"]["lines"][0]
        self.assertEqual(line["line_kind"], "SERVICE")
        self.assertEqual(line["service_tier"], "T1")
        self.assertEqual(line["elapsed_minutes"], 75)
        self.assertEqual(line["billable_minutes"], 90)
        self.assertEqual(line["base_unit_price"], "45.00")
        self.assertEqual(line["unit_price"], "22.00")
        self.assertEqual(line["tariff_version"], "T1T2-v1-2026-09-04")
        self.assertNotIn("requested_discount_percent", line)
        with self.engine.connect() as connection:
            invoice_count = connection.execute(
                select(func.count()).select_from(invoices)
            ).scalar_one()
            active_t1 = connection.execute(select(service_tariffs).where(
                service_tariffs.c.tier == "T1",
                service_tariffs.c.active.is_(True),
            )).one()
            audit = connection.execute(select(direct_sale_discount_audit)).one()
        self.assertEqual(invoice_count, 1)
        self.assertEqual(str(active_t1.first_hour_rate), "30.00")
        self.assertEqual(audit.decision, "CLAMPED_TO_MINIMUM")

    def test_direct_cash_sale_fiscalizes_without_a_service_order(self):
        from sqlalchemy import select
        from mikrofix_server.fiscal_worker import process_fiscal_invoice
        from mikrofix_server.models import invoices

        class ConfirmingProvider:
            def __init__(self):
                self.submissions = []

            def prepare_request(self, request, *, message_id):
                self.zoi = "b" * 32
                return {"InvoiceRequest": {
                    "Header": {"MessageID": message_id},
                    "Invoice": {"ProtectedID": self.zoi},
                }}

            def submit_prepared(self, payload):
                self.submissions.append(payload)
                return {
                    "status": "CONFIRMED", "zoi": self.zoi,
                    "eor": "EOR-DIRECT-1",
                    "response": {"InvoiceResponse": {"EOR": "EOR-DIRECT-1"}},
                }

        supplier_id, article_id = self.seed_supplier_and_article()
        receipt = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=227,
            document_type="DRA", external_no="DRA-CASH-SALE",
        )
        self.assertEqual(self.client.post(
            "/v1/sync/operations", json=receipt, headers=self.headers
        ).json()["result_code"], "ACCEPTED")
        opened = self.client.post(
            "/v1/sync/operations", json=self.open_cash_operation(sequence=227),
            headers=self.headers,
        )
        self.assertEqual(opened.json()["result_code"], "ACCEPTED")
        issue = self.direct_sale_operation(
            uuid4(), sequence=228, amount="15.00", method="CASH", confirmed=True,
        )
        issued = self.client.post(
            "/v1/sync/operations", json=issue, headers=self.headers
        ).json()
        self.assertEqual(
            issued["result"]["invoice_status"], "PENDING_FISCALIZATION"
        )
        invoice_id = issued["result"]["invoice_id"]
        provider = ConfirmingProvider()
        confirmed = process_fiscal_invoice(self.engine, invoice_id, provider)
        repeated = process_fiscal_invoice(self.engine, invoice_id, provider)
        self.assertEqual(confirmed["status"], "ISSUED")
        self.assertEqual(repeated, confirmed)
        self.assertEqual(len(provider.submissions), 1)
        with self.engine.connect() as connection:
            invoice = connection.execute(select(invoices)).one()
        self.assertEqual(invoice.source_type, "DIRECT_SALE")
        self.assertEqual(invoice.furs_eor, "EOR-DIRECT-1")

    def test_service_scan_consumes_the_released_supplier_identity_fifo(self):
        from sqlalchemy import select
        from mikrofix_server.models import (
            inventory_items, inventory_lots, service_stock_allocations,
        )

        supplier_id, article_id = self.seed_supplier_and_article()
        receipt = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=231,
            document_type="DRA", external_no="DRA-FOR-SERVICE",
        )
        accepted = self.client.post(
            "/v1/sync/operations", json=receipt, headers=self.headers
        ).json()
        self.assertEqual(accepted["result_code"], "ACCEPTED")

        order = self.operation(sequence=232)
        self.client.post("/v1/sync/operations", json=order, headers=self.headers)
        completion = self.completion_operation(
            order, sequence=233, barcode="SUP-BARCODE-1", article_id=article_id,
        )
        # The current SQLite Service carries a local integer row id.  The
        # central stock identity is resolved from the scanned supplier barcode.
        completion["payload"]["parts"][0]["article_id"] = "17"
        completion["payload"]["parts"][0]["lot_serial"] = ""
        completed = self.client.post(
            "/v1/sync/operations", json=completion, headers=self.headers
        ).json()
        self.assertEqual(completed["result_code"], "ACCEPTED")
        with self.engine.connect() as connection:
            stock = connection.execute(select(inventory_items).where(
                inventory_items.c.article_uuid == article_id
            )).one()
            lot = connection.execute(select(inventory_lots)).one()
            allocation = connection.execute(select(service_stock_allocations)).one()
        self.assertEqual(str(stock.quantity_on_hand), "1.0000")
        self.assertEqual(str(lot.available_quantity), "1.0000")
        self.assertEqual(lot.status, "AVAILABLE")
        self.assertEqual(str(allocation.quantity), "1.0000")

    def test_serial_tracked_receipt_requires_one_named_serial(self):
        supplier_id, article_id = self.seed_supplier_and_article(track_mode="SERIAL")
        receipt = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=241,
            document_type="DRA", external_no="SERIAL-MISSING",
        )
        response = self.client.post(
            "/v1/sync/operations", json=receipt, headers=self.headers
        ).json()
        self.assertEqual(response["result_code"], "REJECTED")
        self.assertEqual(response["error_code"], "INVALID_RECEIPT")
        self.assertIn("serial-tracked", response["error_message"])

    def test_office_receipt_uses_central_session_actor_and_read_model(self):
        from sqlalchemy import select
        from mikrofix_server.models import accepted_operations

        login_response = self.central_login(role="OFFICE", application="OFFICE")
        self.assertEqual(login_response.status_code, 200, login_response.text)
        headers = {
            "Authorization": f"Bearer {login_response.json()['access_token']}"
        }
        supplier_id, article_id = self.seed_supplier_and_article()
        receipt = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=1,
            document_type="DRA", external_no="OFFICE-AUTH-DRA",
        )
        receipt["actor_id"] = "forged-office"
        receipt["device_id"] = "FORGED-OFFICE-PC"

        response = self.client.post(
            "/v1/sync/operations", json=receipt, headers=headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result_code"], "ACCEPTED")
        inventory = self.client.get(
            "/v1/office/inventory?q=SUP-BARCODE-1", headers=headers
        )
        self.assertEqual(inventory.status_code, 200, inventory.text)
        self.assertEqual(
            inventory.json()["inventory"][0]["identity_quantity_available"],
            "2.0000",
        )
        receipt_list = self.client.get(
            "/v1/office/purchase-receipts", headers=headers
        )
        self.assertEqual(receipt_list.status_code, 200, receipt_list.text)
        read_model = receipt_list.json()["receipts"][0]
        self.assertEqual(read_model["invoice_due_date"], "2026-10-12")
        self.assertEqual(read_model["invoice_payment_status"], "NEPLACAN")
        self.assertEqual(read_model["lines"][0]["net_unit_price"], "10.0000")
        self.assertEqual(read_model["lines"][0]["discount_percent"], "0.0000")
        self.assertEqual(read_model["lines"][0]["vat_rate"], "11.0000")
        self.assertEqual(read_model["lines"][0]["supplier_line_total"], "22.20")
        with self.engine.connect() as connection:
            operation = connection.execute(select(accepted_operations).where(
                accepted_operations.c.operation_id == receipt["operation_id"]
            )).one()
        self.assertEqual(operation.actor_id, "office-auth")
        self.assertEqual(operation.device_id, "OFFICE-PC-OFFICE")

    def test_office_cannot_confirm_receipt_without_document_permission(self):
        from mikrofix_server.auth import create_central_user, register_workstation

        with self.engine.begin() as connection:
            user_id = create_central_user(
                connection,
                username="office-read-only",
                display_name="Office Read Only",
                role="OFFICE",
                pin="246810",
                permissions={"MODULE_OFFICE"},
                must_change_pin=False,
            )
            _, secret = register_workstation(
                connection,
                workstation_code="OFFICE-PC-READ-ONLY",
                display_name="Office read-only PC",
                application_type="OFFICE",
                approved_by=user_id,
            )
        login = self.client.post("/v1/auth/login", json={
            "username": "office-read-only",
            "pin": "246810",
            "workstation_code": "OFFICE-PC-READ-ONLY",
            "workstation_secret": secret,
            "application_type": "OFFICE",
        })
        self.assertEqual(login.status_code, 200, login.text)
        supplier_id, article_id = self.seed_supplier_and_article()
        receipt = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=1,
            document_type="DRA", external_no="OFFICE-NO-CONFIRM",
        )

        response = self.client.post(
            "/v1/sync/operations",
            json=receipt,
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("CONFIRM_DOCUMENT", response.json()["detail"])

    def test_office_creates_zero_stock_article_and_duplicate_barcode_is_rejected(self):
        from sqlalchemy import func, select
        from mikrofix_server.models import inventory_items

        login = self.central_login(role="OFFICE", application="OFFICE")
        self.assertEqual(login.status_code, 200, login.text)
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        first = self.create_inventory_item_operation(
            sequence=1, barcode="OFFICE-NEW-ARTICLE",
        )
        created = self.client.post(
            "/v1/sync/operations", json=first, headers=headers,
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["result_code"], "ACCEPTED")

        inventory = self.client.get(
            "/v1/office/inventory", params={"q": "OFFICE-NEW-ARTICLE"},
            headers=headers,
        )
        self.assertEqual(inventory.status_code, 200, inventory.text)
        item = inventory.json()["inventory"][0]
        self.assertEqual(item["article_id"], first["aggregate_id"])
        self.assertEqual(item["article_quantity_available"], "0.0000")
        self.assertEqual(item["current_sale_price"], "0.00")

        duplicate = self.create_inventory_item_operation(
            sequence=2, barcode="OFFICE-NEW-ARTICLE",
        )
        rejected = self.client.post(
            "/v1/sync/operations", json=duplicate, headers=headers,
        )
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertEqual(rejected.json()["result_code"], "REJECTED")
        self.assertEqual(
            rejected.json()["error_code"], "INVENTORY_ITEM_ALREADY_EXISTS",
        )
        self.assertEqual(
            rejected.json()["result"]["existing_article_id"],
            first["aggregate_id"],
        )
        with self.engine.connect() as connection:
            count = connection.execute(
                select(func.count()).select_from(inventory_items)
            ).scalar_one()
        self.assertEqual(count, 1)

    def test_office_cannot_create_article_without_inventory_permission(self):
        from mikrofix_server.auth import create_central_user, register_workstation

        with self.engine.begin() as connection:
            user_id = create_central_user(
                connection, username="office-no-inventory",
                display_name="Office brez zaloge", role="OFFICE", pin="246810",
                permissions={"MODULE_OFFICE"}, must_change_pin=False,
            )
            _, secret = register_workstation(
                connection, workstation_code="OFFICE-PC-NO-INVENTORY",
                display_name="Office brez zaloge PC", application_type="OFFICE",
                approved_by=user_id,
            )
        login = self.client.post("/v1/auth/login", json={
            "username": "office-no-inventory", "pin": "246810",
            "workstation_code": "OFFICE-PC-NO-INVENTORY",
            "workstation_secret": secret, "application_type": "OFFICE",
        })
        self.assertEqual(login.status_code, 200, login.text)
        response = self.client.post(
            "/v1/sync/operations",
            json=self.create_inventory_item_operation(sequence=1),
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("MANAGE_INVENTORY", response.json()["detail"])

    def test_office_price_change_is_versioned_cost_guarded_and_audited(self):
        from sqlalchemy import select
        from mikrofix_server.models import inventory_items, inventory_price_history

        supplier_id, article_id = self.seed_supplier_and_article()
        receipt = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=1,
            document_type="DRA", external_no="DRA-BEFORE-PRICE-CHANGE",
        )
        released = self.client.post(
            "/v1/sync/operations", json=receipt, headers=self.headers,
        ).json()
        self.assertEqual(released["result_code"], "ACCEPTED")
        login = self.central_login(role="OFFICE", application="OFFICE")
        self.assertEqual(login.status_code, 200, login.text)
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        changed_request = self.update_inventory_price_operation(
            article_id, sequence=1, expected_version=2, price="14.00",
            reason="Nova prodajna odločitev",
        )
        changed = self.client.post(
            "/v1/sync/operations", json=changed_request, headers=headers,
        ).json()
        self.assertEqual(changed["result_code"], "ACCEPTED")
        self.assertEqual(changed["result"]["row_version"], 3)
        self.assertEqual(changed["result"]["minimum_sale_price"], "12.71")

        unchanged = self.client.post(
            "/v1/sync/operations",
            json=self.update_inventory_price_operation(
                article_id, sequence=2, expected_version=3, price="14.00",
            ),
            headers=headers,
        ).json()
        self.assertEqual(unchanged["result_code"], "REJECTED")
        self.assertEqual(unchanged["error_code"], "SALE_PRICE_UNCHANGED")

        too_low = self.client.post(
            "/v1/sync/operations",
            json=self.update_inventory_price_operation(
                article_id, sequence=3, expected_version=3, price="12.70",
            ),
            headers=headers,
        ).json()
        self.assertEqual(too_low["result_code"], "REJECTED")
        self.assertEqual(too_low["error_code"], "SALE_PRICE_BELOW_AVAILABLE_COST")

        stale = self.client.post(
            "/v1/sync/operations",
            json=self.update_inventory_price_operation(
                article_id, sequence=4, expected_version=2, price="20.00",
            ),
            headers=headers,
        ).json()
        self.assertEqual(stale["result_code"], "REJECTED")
        self.assertEqual(stale["error_code"], "VERSION_CONFLICT")
        history_response = self.client.get(
            f"/v1/office/inventory/{article_id}/price-history", headers=headers,
        )
        self.assertEqual(history_response.status_code, 200, history_response.text)
        history_read_model = history_response.json()["price_history"]
        self.assertEqual(len(history_read_model), 2)
        self.assertEqual(history_read_model[0]["source"], "OFFICE_DECISION")
        self.assertEqual(history_read_model[0]["actor"], "Office Auth")
        self.assertEqual(
            history_read_model[0]["reason"], "Nova prodajna odločitev"
        )
        self.assertTrue(history_read_model[1]["receipt_number"].startswith("DRA-"))
        with self.engine.connect() as connection:
            article = connection.execute(select(inventory_items)).one()
            history = connection.execute(select(inventory_price_history).order_by(
                inventory_price_history.c.effective_at
            )).all()
        self.assertEqual(str(article.current_sale_price), "14.00")
        self.assertEqual(article.row_version, 3)
        self.assertEqual(len(history), 2)
        self.assertIsNone(history[-1].receipt_uuid)
        self.assertEqual(
            str(history[-1].price_change_operation_id),
            changed_request["operation_id"],
        )
        self.assertEqual(history[-1].change_reason, "Nova prodajna odločitev")

    def test_office_cannot_change_price_without_price_permission(self):
        from mikrofix_server.auth import create_central_user, register_workstation

        _, article_id = self.seed_supplier_and_article()
        with self.engine.begin() as connection:
            user_id = create_central_user(
                connection, username="office-no-prices",
                display_name="Office brez cen", role="OFFICE", pin="246810",
                permissions={"MODULE_OFFICE"}, must_change_pin=False,
            )
            _, secret = register_workstation(
                connection, workstation_code="OFFICE-PC-NO-PRICES",
                display_name="Office brez cen PC", application_type="OFFICE",
                approved_by=user_id,
            )
        login = self.client.post("/v1/auth/login", json={
            "username": "office-no-prices", "pin": "246810",
            "workstation_code": "OFFICE-PC-NO-PRICES",
            "workstation_secret": secret, "application_type": "OFFICE",
        })
        self.assertEqual(login.status_code, 200, login.text)
        response = self.client.post(
            "/v1/sync/operations",
            json=self.update_inventory_price_operation(
                article_id, sequence=1, expected_version=1, price="20",
            ),
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("MANAGE_PRICES", response.json()["detail"])

    def test_office_invoice_history_is_read_only_and_session_scoped(self):
        _, draft_id = self.prepared_draft(sequence=1, barcode="OFFICE-HISTORY")
        issued = self.client.post(
            "/v1/sync/operations",
            json=self.issue_operation(draft_id, sequence=3),
            headers=self.headers,
        ).json()
        invoice_id = issued["result"]["invoice_id"]
        login = self.central_login(role="OFFICE", application="OFFICE")
        self.assertEqual(login.status_code, 200, login.text)
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        history = self.client.get(
            "/v1/office/invoices", params={"q": "OFFICE-HISTORY"},
            headers=headers,
        )
        invoice = self.client.get(
            f"/v1/office/invoices/{invoice_id}", headers=headers,
        )

        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()["invoices"][0]["invoice_id"], invoice_id)
        self.assertEqual(invoice.status_code, 200, invoice.text)
        self.assertEqual(invoice.json()["invoice_id"], invoice_id)
        self.assertEqual(
            self.client.get("/v1/cashier/invoices", headers=headers).status_code,
            403,
        )

    def test_office_updates_and_deactivates_partner_without_rewriting_invoice(self):
        login = self.central_login(role="OFFICE", application="OFFICE")
        self.assertEqual(login.status_code, 200, login.text)
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        created = self.create_partner_operation(sequence=1, tax_number="87654321")
        created["operation_type"] = "CreateOfficePartner"
        partner_id = created["aggregate_id"]
        created_response = self.client.post(
            "/v1/sync/operations", json=created, headers=headers,
        ).json()
        self.assertEqual(created_response["result_code"], "ACCEPTED")

        update_payload = dict(created["payload"])
        update_payload.update(
            expected_version=1, name="Posodobljen kupec d.o.o.",
            address="Nova cesta 2", source_reference="OFFICE_EDIT",
        )
        update_request = {
            **created,
            "operation_id": str(uuid4()), "operation_type": "UpdatePartner",
            "expected_version": 1, "device_sequence": 2,
            "payload": update_payload,
        }
        updated = self.client.post(
            "/v1/sync/operations", json=update_request, headers=headers,
        ).json()
        self.assertEqual(updated["result_code"], "ACCEPTED")
        self.assertEqual(updated["aggregate_version"], 2)

        _, draft_id = self.prepared_draft(sequence=301, barcode="PARTNER-SNAPSHOT")
        issue = self.issue_operation(draft_id, sequence=303)
        issue["payload"]["billing_customer"] = {
            "source": "CENTRAL_PARTNER", "confirmed": True,
            "partner_id": partner_id, "expected_version": 2,
        }
        issued = self.client.post(
            "/v1/sync/operations", json=issue, headers=self.headers,
        ).json()
        self.assertEqual(issued["result_code"], "ACCEPTED")
        invoice_id = issued["result"]["invoice_id"]

        status_request = {
            **created,
            "operation_id": str(uuid4()), "operation_type": "SetPartnerActive",
            "expected_version": 2, "device_sequence": 3,
            "payload": {
                "partner_id": partner_id, "expected_version": 2,
                "active": False, "reason": "Partner je podvojen",
            },
        }
        deactivated = self.client.post(
            "/v1/sync/operations", json=status_request, headers=headers,
        ).json()
        self.assertEqual(deactivated["result_code"], "ACCEPTED")
        self.assertFalse(deactivated["result"]["active"])

        stale = {
            **update_request,
            "operation_id": str(uuid4()), "device_sequence": 4,
        }
        stale_response = self.client.post(
            "/v1/sync/operations", json=stale, headers=headers,
        ).json()
        self.assertEqual(stale_response["result_code"], "REJECTED")
        self.assertEqual(stale_response["error_code"], "VERSION_CONFLICT")

        office_rows = self.client.get(
            "/v1/office/partners", params={"q": "87654321"}, headers=headers,
        ).json()["partners"]
        cashier_rows = self.client.get(
            "/v1/cashier/partners", params={"q": "87654321"}, headers=self.headers,
        ).json()["partners"]
        invoice = self.client.get(
            f"/v1/office/invoices/{invoice_id}", headers=headers,
        ).json()
        self.assertEqual(office_rows[0]["row_version"], 3)
        self.assertFalse(office_rows[0]["active"])
        self.assertEqual(cashier_rows, [])
        self.assertEqual(
            invoice["snapshot"]["billing_customer"]["name"],
            "Posodobljen kupec d.o.o.",
        )

    def test_concurrent_partner_edits_have_one_winner_and_tax_identity_is_unique(self):
        first = self.create_partner_operation(sequence=401, tax_number="11112222")
        second = self.create_partner_operation(sequence=402, tax_number="33334444")
        self.assertEqual(self.client.post(
            "/v1/sync/operations", json=first, headers=self.headers,
        ).json()["result_code"], "ACCEPTED")
        self.assertEqual(self.client.post(
            "/v1/sync/operations", json=second, headers=self.headers,
        ).json()["result_code"], "ACCEPTED")

        edits = []
        for sequence, name in ((403, "Prva sprememba"), (404, "Druga sprememba")):
            payload = dict(first["payload"])
            payload.update(
                expected_version=1, name=name, source_reference="OFFICE_EDIT",
            )
            edits.append({
                **first, "operation_id": str(uuid4()),
                "operation_type": "UpdatePartner", "expected_version": 1,
                "device_sequence": sequence, "payload": payload,
            })

        def submit(request):
            return self.client.post(
                "/v1/sync/operations", json=request, headers=self.headers,
            ).json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, edits))
        self.assertEqual(
            sorted(result["result_code"] for result in results),
            ["ACCEPTED", "REJECTED"],
        )
        rejected = next(result for result in results if result["result_code"] == "REJECTED")
        self.assertEqual(rejected["error_code"], "VERSION_CONFLICT")

        duplicate_payload = dict(second["payload"])
        duplicate_payload.update(
            expected_version=1, tax_number="11112222",
            source_reference="OFFICE_EDIT",
        )
        duplicate = {
            **second, "operation_id": str(uuid4()),
            "operation_type": "UpdatePartner", "expected_version": 1,
            "device_sequence": 405, "payload": duplicate_payload,
        }
        duplicate_response = self.client.post(
            "/v1/sync/operations", json=duplicate, headers=self.headers,
        ).json()
        self.assertEqual(duplicate_response["result_code"], "REJECTED")
        self.assertEqual(
            duplicate_response["error_code"], "PARTNER_TAX_NUMBER_EXISTS"
        )

    def test_late_receipt_rejection_rolls_back_every_stock_side_effect(self):
        from sqlalchemy import func, select
        from mikrofix_server.models import (
            accepted_operations, inventory_identities, inventory_lots,
            purchase_receipt_lines, purchase_receipts, receipt_number_sequences,
        )

        supplier_id, article_id = self.seed_supplier_and_article()
        receipt = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=251,
            document_type="DRA", external_no="TWO-PRICES",
        )
        second = dict(receipt["payload"]["lines"][0])
        second.update(
            line_id=str(uuid4()), supplier_barcode="SUP-BARCODE-2",
            confirmed_sale_price="16.00",
        )
        receipt["payload"]["lines"].append(second)
        receipt["payload"]["document_total"] = "46.40"

        response = self.client.post(
            "/v1/sync/operations", json=receipt, headers=self.headers
        ).json()
        self.assertEqual(response["result_code"], "REJECTED")
        self.assertEqual(response["error_code"], "INVALID_RECEIPT")
        with self.engine.connect() as connection:
            for table in (
                purchase_receipts, purchase_receipt_lines,
                inventory_identities, inventory_lots, receipt_number_sequences,
            ):
                self.assertEqual(connection.execute(
                    select(func.count()).select_from(table)
                ).scalar_one(), 0)
            operation = connection.execute(select(accepted_operations)).one()
        self.assertEqual(operation.result_code, "REJECTED")

    def test_pra_rejection_preserves_the_original_pending_dob(self):
        from sqlalchemy import func, select
        from mikrofix_server.models import (
            inventory_items, inventory_lots, purchase_receipt_lines,
            purchase_receipt_revisions, purchase_receipts, receipt_number_sequences,
        )

        supplier_id, article_id = self.seed_supplier_and_article()
        receipt_id = uuid4()
        initial = self.purchase_receipt_operation(
            receipt_id, supplier_id, article_id, sequence=261,
            external_no="DOB-BEFORE-BAD-PRA",
        )
        self.client.post("/v1/sync/operations", json=initial, headers=self.headers)
        second = dict(initial["payload"]["lines"][0])
        second.update(
            line_id=str(uuid4()), supplier_barcode="SUP-BARCODE-2",
            confirmed_sale_price="16.00",
        )
        invoice = {
            "operation_id": str(uuid4()), "operation_type": "AttachPurchaseInvoice",
            "aggregate_type": "PURCHASE_RECEIPT", "aggregate_id": str(receipt_id),
            "expected_version": 1, "actor_id": "office-test",
            "device_id": "OFFICE-PC-TEST", "device_sequence": 262,
            "occurred_at": "2026-09-12T12:00:00+02:00", "contract_version": 1,
            "payload": {
                "receipt_id": str(receipt_id), "expected_version": 1,
                "invoice_external_no": "BAD-PRA-1", "invoice_date": "2026-09-12",
                "invoice_due_date": "2026-10-12", "invoice_payment_status": "NEPLACAN",
                "currency": "EUR", "document_total": "46.40", "shipping_total": "2",
                "document_review_confirmed": True,
                "lines": [initial["payload"]["lines"][0], second], "attachments": [],
            },
        }
        rejected = self.client.post(
            "/v1/sync/operations", json=invoice, headers=self.headers
        ).json()
        self.assertEqual(rejected["result_code"], "REJECTED")
        with self.engine.connect() as connection:
            receipt = connection.execute(select(purchase_receipts)).one()
            lot = connection.execute(select(inventory_lots)).one()
            stock = connection.execute(select(inventory_items).where(
                inventory_items.c.article_uuid == article_id
            )).one()
            self.assertEqual(connection.execute(
                select(func.count()).select_from(purchase_receipt_lines)
            ).scalar_one(), 1)
            self.assertEqual(connection.execute(
                select(func.count()).select_from(purchase_receipt_revisions)
            ).scalar_one(), 1)
            self.assertEqual(connection.execute(select(func.count()).select_from(
                receipt_number_sequences
            ).where(receipt_number_sequences.c.series_code == "PRA")).scalar_one(), 0)
        self.assertEqual(receipt.status, "PENDING_INVOICE")
        self.assertEqual(receipt.row_version, 1)
        self.assertEqual(lot.status, "PENDING")
        self.assertEqual(str(stock.quantity_on_hand), "0.0000")

    def test_new_price_cannot_undercut_highest_available_lot_cost(self):
        from sqlalchemy import func, select
        from mikrofix_server.models import inventory_items, purchase_receipts

        supplier_id, article_id = self.seed_supplier_and_article()
        first = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=271,
            document_type="DRA", external_no="HIGH-COST",
        )
        self.client.post("/v1/sync/operations", json=first, headers=self.headers)
        second = self.purchase_receipt_operation(
            uuid4(), supplier_id, article_id, sequence=272,
            document_type="DRA", external_no="LOW-COST", sale_price="12.70",
        )
        line = second["payload"]["lines"][0]
        line.update(
            net_unit_price="5", vat_rate="0", supplier_line_total="10.00",
            supplier_barcode="LOW-COST-BARCODE",
        )
        second["payload"].update(document_total="10.00", shipping_total="0")
        rejected = self.client.post(
            "/v1/sync/operations", json=second, headers=self.headers
        ).json()
        self.assertEqual(rejected["result_code"], "REJECTED")
        self.assertIn("12.71", rejected["error_message"])
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(
                select(func.count()).select_from(purchase_receipts)
            ).scalar_one(), 1)
            stock = connection.execute(select(inventory_items).where(
                inventory_items.c.article_uuid == article_id
            )).one()
        self.assertEqual(str(stock.quantity_on_hand), "2.0000")
        self.assertEqual(str(stock.current_sale_price), "15.00")

    def test_inventory_preparation_blocks_new_business_until_cancelled(self):
        session_id = uuid4()
        begin = self.full_inventory_operation(
            session_id,
            operation_type="BeginFullInventoryPreparation",
            sequence=1,
            expected_version=0,
            payload={
                "session_id": str(session_id),
                "reference": "INVENTURA-2026-09",
                "confirmed": True,
            },
        )
        started = self.client.post(
            "/v1/sync/operations", json=begin, headers=self.headers
        ).json()
        self.assertEqual(started["result_code"], "ACCEPTED")
        self.assertEqual(started["result"]["status"], "PREPARING")

        retried = self.client.post(
            "/v1/sync/operations", json=begin, headers=self.headers
        ).json()
        self.assertEqual(retried, started)

        blocked_order = self.operation(sequence=2)
        blocked = self.client.post(
            "/v1/sync/operations", json=blocked_order, headers=self.headers
        ).json()
        self.assertEqual(blocked["result_code"], "REJECTED")
        self.assertEqual(blocked["error_code"], "FULL_INVENTORY_LOCKED")

        cancel = self.full_inventory_operation(
            session_id,
            operation_type="CancelFullInventoryPreparation",
            sequence=3,
            expected_version=1,
            payload={
                "session_id": str(session_id),
                "reason": "Priprava je bila prekinjena zaradi nedosegljive postaje",
                "confirmed": True,
            },
        )
        cancelled = self.client.post(
            "/v1/sync/operations", json=cancel, headers=self.headers
        ).json()
        self.assertEqual(cancelled["result_code"], "ACCEPTED")
        self.assertEqual(cancelled["result"]["status"], "CANCELLED")

        accepted_order = self.operation(sequence=4)
        accepted = self.client.post(
            "/v1/sync/operations", json=accepted_order, headers=self.headers
        ).json()
        self.assertEqual(accepted["result_code"], "ACCEPTED")

    def test_service_readiness_is_bound_to_session_and_keeps_blockers(self):
        from datetime import datetime, timezone

        from sqlalchemy import insert, select

        from mikrofix_server.models import inventory_service_readiness, workstations
        from mikrofix_server.operations import process_operation
        from mikrofix_sync_contracts import SyncOperationEnvelope

        session_id = uuid4()
        begin = self.full_inventory_operation(
            session_id,
            operation_type="BeginFullInventoryPreparation",
            sequence=10,
            expected_version=0,
            payload={
                "session_id": str(session_id),
                "reference": "INVENTURA-READINESS",
                "confirmed": True,
            },
        )
        started = self.client.post(
            "/v1/sync/operations", json=begin, headers=self.headers
        ).json()
        barrier = int(started["result"]["barrier_sequence"])
        workstation_id = uuid4()
        with self.engine.begin() as connection:
            connection.execute(insert(workstations).values(
                workstation_uuid=workstation_id,
                workstation_code="SERVICE-READY-1",
                display_name="Service readiness test",
                application_type="SERVICE",
                credential_hash="0" * 64,
                is_active=True,
                approved_at=datetime.now(timezone.utc),
            ))
            request = SyncOperationEnvelope(
                operation_id=str(uuid4()),
                operation_type="AcknowledgeFullInventoryReadiness",
                aggregate_type="FULL_INVENTORY",
                aggregate_id=str(session_id),
                expected_version=1,
                actor_id="serviser-test",
                device_id="SERVICE-READY-1",
                device_sequence=1,
                occurred_at="2026-09-19T08:10:00+02:00",
                payload={
                    "session_id": str(session_id),
                    "pending_outbox_count": 1,
                    "uncertain_outbox_count": 0,
                    "sync_conflict_count": 0,
                    "central_checkpoint": barrier,
                    "local_write_lock_active": True,
                },
            )
            receipt = process_operation(
                connection,
                request,
                transmitted_by_workstation_uuid=workstation_id,
            )
            row = connection.execute(
                select(inventory_service_readiness).where(
                    inventory_service_readiness.c.session_uuid == session_id,
                    inventory_service_readiness.c.workstation_uuid == workstation_id,
                )
            ).one()
        self.assertEqual(receipt["result_code"], "ACCEPTED")
        self.assertFalse(receipt["result"]["ready"])
        self.assertEqual(row.pending_outbox_count, 1)
        self.assertTrue(row.local_write_lock_active)


if __name__ == "__main__":
    unittest.main()
