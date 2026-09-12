"""FastAPI application factory for the central MikroFIX prototype."""

from __future__ import annotations

import base64
import binascii
import secrets
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from sqlalchemy import Engine, func, or_, select, text
from sqlalchemy.exc import IntegrityError

from mikrofix_sync_contracts import ContractError, SyncOperationEnvelope
from mikrofix_ajpes import AjpesError, find_partner_by_tax_number
from mikrofix_device_signing import verify_operation_signature

from .config import ServerSettings
from .auth import (
    AuthenticationError,
    AuthorizationError,
    RequestIdentity,
    assert_identity_current,
    change_own_pin,
    login,
    create_central_user,
    register_workstation,
    resolve_session,
    revoke_session,
    set_workstation_active,
    update_user_access,
)
from .database import create_database_engine
from .models import (
    accepted_operations,
    central_user_permissions,
    central_user_permission_history,
    central_users,
    change_feed,
    inventory_identities,
    inventory_items,
    inventory_lots,
    invoice_drafts,
    partners,
    purchase_receipt_lines,
    purchase_receipts,
    receipt_document_types,
    workstations,
)
from .operations import (
    ProtocolConflict, _inventory_updates_for_articles, process_operation,
)
from .schemas import (
    ChangePage,
    ChangeOwnPinRequest,
    CreateCentralUserRequest,
    LoginRequest,
    LoginResponse,
    OperationReceipt,
    OperationRequest,
    RegisterWorkstationRequest,
    SetWorkstationActiveRequest,
    UpdateCentralUserAccessRequest,
)


def create_app(
    settings: ServerSettings | None = None,
    *,
    engine: Engine | None = None,
) -> FastAPI:
    configured = settings or ServerSettings.from_environment()
    database_engine = engine or create_database_engine(configured.database_url)
    app = FastAPI(title="MikroFIX Central API", version="0.1.0")
    app.state.settings = configured
    app.state.engine = database_engine

    def bearer_token(authorization: str | None) -> str:
        prefix = "Bearer "
        if not authorization or not authorization.startswith(prefix):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing bearer credentials.",
            )
        return authorization[len(prefix):].strip()

    def authenticated_identity(
        authorization: str | None = Header(default=None),
    ) -> RequestIdentity:
        token = bearer_token(authorization)
        if (
            configured.environment.lower() != "production"
            and configured.sync_token
            and secrets.compare_digest(token, configured.sync_token)
        ):
            return RequestIdentity(
                user_uuid=None, username="development-token",
                display_name="Development compatibility token", role="DEVELOPMENT",
                permissions=frozenset(), workstation_uuid=None,
                workstation_code="", application_type="",
                operator_tax_number="", legacy_development=True,
                user_version=0, signing_public_key=None, must_change_pin=False,
            )
        try:
            with database_engine.begin() as connection:
                return resolve_session(connection, token)
        except AuthenticationError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
            ) from exc

    def require_application(
        permission: str, application: str, *, allow_legacy: bool = True
    ):
        def dependency(
            identity: RequestIdentity = Depends(authenticated_identity),
        ) -> RequestIdentity:
            if identity.legacy_development:
                if allow_legacy:
                    return identity
                raise HTTPException(
                    status_code=403,
                    detail="Development compatibility token cannot administer access.",
                )
            if identity.must_change_pin:
                raise HTTPException(
                    status_code=403, detail="PIN change is required before application use."
                )
            try:
                identity.require(permission)
            except AuthorizationError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            if identity.application_type != application:
                raise HTTPException(
                    status_code=403,
                    detail=f"Session is not bound to the {application} application.",
                )
            return identity
        return dependency

    service_identity = require_application("MODULE_SERVICE", "SERVICE")
    cashier_identity = require_application("MODULE_CASHIER", "CASHIER")
    office_identity = require_application("MODULE_OFFICE", "OFFICE")
    admin_identity = require_application(
        "MANAGE_USERS", "OFFICE", allow_legacy=False
    )

    @app.post("/v1/auth/login", response_model=LoginResponse)
    def central_login(request: LoginRequest) -> dict:
        error: Exception | None = None
        result = None
        with database_engine.begin() as connection:
            try:
                result = login(
                    connection,
                    username=request.username,
                    pin=request.pin,
                    workstation_code=request.workstation_code,
                    workstation_secret=request.workstation_secret,
                    application_type=request.application_type,
                    session_minutes=configured.session_minutes,
                )
            except (AuthenticationError, AuthorizationError) as exc:
                error = exc
        if error:
            code = 403 if isinstance(error, AuthorizationError) else 401
            raise HTTPException(status_code=code, detail=str(error))
        token, identity, expires_at, must_change_pin = result
        return {
            "access_token": token,
            "expires_at": expires_at,
            "user_id": identity.user_uuid,
            "user_version": identity.user_version,
            "username": identity.username,
            "display_name": identity.display_name,
            "role": identity.role,
            "workstation_code": identity.workstation_code,
            "application_type": identity.application_type,
            "permissions": sorted(identity.permissions),
            "must_change_pin": must_change_pin,
        }

    @app.post("/v1/auth/logout", status_code=204)
    def central_logout(
        authorization: str | None = Header(default=None),
        identity: RequestIdentity = Depends(authenticated_identity),
    ) -> None:
        if identity.legacy_development:
            raise HTTPException(status_code=403, detail="Development token has no session.")
        token = bearer_token(authorization)
        with database_engine.begin() as connection:
            revoke_session(connection, token, identity)

    @app.post("/v1/auth/change-pin", status_code=204)
    def central_change_pin(
        request: ChangeOwnPinRequest,
        identity: RequestIdentity = Depends(authenticated_identity),
    ) -> None:
        if identity.legacy_development:
            raise HTTPException(status_code=403, detail="Development token has no PIN.")
        try:
            with database_engine.begin() as connection:
                change_own_pin(
                    connection, user_uuid=identity.user_uuid,
                    current_pin=request.current_pin, new_pin=request.new_pin,
                    workstation_uuid=identity.workstation_uuid,
                )
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v1/admin/users", dependencies=[Depends(admin_identity)])
    def list_central_users() -> dict:
        with database_engine.connect() as connection:
            rows = connection.execute(
                select(central_users).order_by(
                    central_users.c.username_normalized
                )
            ).mappings().all()
            permissions = connection.execute(
                select(
                    central_user_permissions.c.user_uuid,
                    central_user_permissions.c.permission,
                )
            ).all()
        by_user: dict[UUID, list[str]] = {}
        for user_uuid, permission in permissions:
            by_user.setdefault(user_uuid, []).append(str(permission))
        return {"users": [{
            "user_id": row["user_uuid"], "username": row["username"],
            "display_name": row["display_name"], "role": row["role"],
            "permissions": sorted(by_user.get(row["user_uuid"], [])),
            "is_active": row["is_active"],
            "operator_tax_number": row["operator_tax_number"] or "",
            "row_version": row["row_version"],
        } for row in rows]}

    @app.post("/v1/admin/users", status_code=201)
    def add_central_user(
        request: CreateCentralUserRequest,
        identity: RequestIdentity = Depends(admin_identity),
    ) -> dict:
        try:
            with database_engine.begin() as connection:
                user_uuid = create_central_user(
                    connection, **request.model_dump(), created_by=identity.user_uuid
                )
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Username already exists.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"user_id": user_uuid}

    @app.put("/v1/admin/users/{user_uuid}/access")
    def change_central_user_access(
        user_uuid: UUID,
        request: UpdateCentralUserAccessRequest,
        identity: RequestIdentity = Depends(admin_identity),
    ) -> dict:
        try:
            with database_engine.begin() as connection:
                version = update_user_access(
                    connection, user_uuid=user_uuid, **request.model_dump(),
                    changed_by=identity.user_uuid,
                )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"user_id": user_uuid, "row_version": version}

    @app.get("/v1/admin/workstations", dependencies=[Depends(admin_identity)])
    def list_central_workstations() -> dict:
        with database_engine.connect() as connection:
            rows = connection.execute(
                select(workstations).order_by(workstations.c.workstation_code)
            ).mappings().all()
        return {"workstations": [{
            "workstation_id": row["workstation_uuid"],
            "workstation_code": row["workstation_code"],
            "display_name": row["display_name"],
            "application_type": row["application_type"],
            "is_active": row["is_active"],
            "has_signing_key": row["signing_public_key"] is not None,
            "approved_at": row["approved_at"],
            "last_seen_at": row["last_seen_at"],
        } for row in rows]}

    @app.post("/v1/admin/workstations", status_code=201)
    def add_central_workstation(
        request: RegisterWorkstationRequest,
        identity: RequestIdentity = Depends(admin_identity),
    ) -> dict:
        try:
            public_key = (
                base64.b64decode(request.signing_public_key, validate=True)
                if request.signing_public_key else None
            )
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Invalid signing public key.") from exc
        try:
            with database_engine.begin() as connection:
                workstation_uuid, raw_secret = register_workstation(
                    connection,
                    workstation_code=request.workstation_code,
                    display_name=request.display_name,
                    application_type=request.application_type,
                    approved_by=identity.user_uuid,
                    signing_public_key=public_key,
                )
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Workstation code already exists.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "workstation_id": workstation_uuid,
            "workstation_secret": raw_secret,
            "secret_shown_once": True,
        }

    @app.patch("/v1/admin/workstations/{workstation_uuid}")
    def change_workstation_access(
        workstation_uuid: UUID,
        request: SetWorkstationActiveRequest,
        identity: RequestIdentity = Depends(admin_identity),
    ) -> dict:
        try:
            with database_engine.begin() as connection:
                set_workstation_active(
                    connection, workstation_uuid=workstation_uuid,
                    is_active=request.is_active, changed_by=identity.user_uuid,
                )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"workstation_id": workstation_uuid, "is_active": request.is_active}

    @app.get("/health")
    def health() -> dict[str, str]:
        with database_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "environment": configured.environment}

    @app.post(
        "/v1/sync/operations",
        response_model=OperationReceipt,
    )
    def accept_operation(
        request: OperationRequest,
        identity: RequestIdentity = Depends(authenticated_identity),
    ) -> dict:
        raw = request.model_dump(mode="json")
        requirements = {
            "CreateServiceOrder": ("MODULE_SERVICE", "SERVICE"),
            "CompleteServiceTechnically": ("MODULE_SERVICE", "SERVICE"),
            "CreatePartner": ("MODULE_CASHIER", "CASHIER"),
            "IssueServiceInvoice": ("MODULE_CASHIER", "CASHIER"),
            "IssueDirectSaleInvoice": ("MODULE_CASHIER", "CASHIER"),
            "ConfirmPurchaseReceipt": ("MODULE_OFFICE", "OFFICE"),
            "AttachPurchaseInvoice": ("MODULE_OFFICE", "OFFICE"),
        }
        required = requirements.get(request.operation_type)
        if identity.must_change_pin and not identity.legacy_development:
            raise HTTPException(
                status_code=403, detail="PIN change is required before operations."
            )
        if required and not identity.legacy_development:
            try:
                identity.require(required[0])
            except AuthorizationError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            if identity.application_type != required[1]:
                raise HTTPException(status_code=403, detail="Wrong application session.")
            raw["device_id"] = identity.workstation_code
            if required[1] == "SERVICE":
                if not raw.get("actor_user_id") or not raw.get("workstation_signature"):
                    raise HTTPException(
                        status_code=403,
                        detail="Offline Service operation has no signed actor identity.",
                    )
            else:
                raw["actor_id"] = identity.username
                raw["actor_user_id"] = identity.user_uuid
                raw["actor_authorization_version"] = identity.user_version
                raw["workstation_signature"] = ""
                if (
                    request.operation_type in {
                        "IssueServiceInvoice", "IssueDirectSaleInvoice"
                    }
                    and any(
                        str(payment.get("method") or "").upper() in {"CASH", "CARD"}
                        for payment in request.payload.get("payments", [])
                    )
                    and not identity.operator_tax_number
                ):
                    raise HTTPException(
                        status_code=403,
                        detail="Cash or card issuance requires the cashier tax number.",
                    )
        try:
            envelope = SyncOperationEnvelope(**raw)
        except ContractError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        signed_service = (
            bool(required) and required[1] == "SERVICE"
            and not identity.legacy_development
        )
        if signed_service:
            if not identity.signing_public_key or not verify_operation_signature(
                envelope, identity.signing_public_key
            ):
                raise HTTPException(
                    status_code=403, detail="Invalid Service workstation signature."
                )
        try:
            with database_engine.begin() as connection:
                if required and not identity.legacy_development:
                    assert_identity_current(
                        connection, identity, permission=required[0],
                        application_type=required[1],
                    )
                if signed_service:
                    actor = connection.execute(
                        select(
                            central_users.c.username_normalized,
                            central_users.c.row_version,
                        ).where(
                            central_users.c.user_uuid == envelope.actor_user_id
                        )
                    ).one_or_none()
                    has_permission = connection.execute(
                        select(central_user_permission_history.c.permission).where(
                            central_user_permission_history.c.user_uuid == envelope.actor_user_id,
                            central_user_permission_history.c.permission == "MODULE_SERVICE",
                            central_user_permission_history.c.valid_from_version
                            <= envelope.actor_authorization_version,
                            or_(
                                central_user_permission_history.c.valid_until_version.is_(None),
                                central_user_permission_history.c.valid_until_version
                                >= envelope.actor_authorization_version,
                            ),
                        )
                    ).scalar_one_or_none()
                    if (
                        not actor
                        or actor.username_normalized != envelope.actor_id.strip().lower()
                        or envelope.actor_authorization_version > actor.row_version
                        or has_permission is None
                    ):
                        raise AuthorizationError(
                            "Signed Service actor is not a known authorized user."
                        )
                return process_operation(
                    connection,
                    envelope,
                    transmitted_by_user_uuid=identity.user_uuid,
                    transmitted_by_workstation_uuid=identity.workstation_uuid,
                    operator_tax_number=identity.operator_tax_number,
                )
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ProtocolConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except IntegrityError as exc:
            operation_id = request.operation_id
            with database_engine.connect() as connection:
                same_sequence = connection.execute(
                    select(accepted_operations.c.operation_id).where(
                        accepted_operations.c.device_id == envelope.device_id,
                        accepted_operations.c.device_sequence == request.device_sequence,
                    )
                ).scalar_one_or_none()
            if same_sequence is not None and same_sequence != operation_id:
                raise HTTPException(
                    status_code=409,
                    detail="device_sequence is already bound to another operation",
                ) from exc
            raise

    @app.get(
        "/v1/sync/changes",
        response_model=ChangePage,
        dependencies=[Depends(service_identity)],
    )
    def pull_changes(
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict:
        with database_engine.connect() as connection:
            rows = connection.execute(
                select(change_feed)
                .where(change_feed.c.central_sequence > after)
                .order_by(change_feed.c.central_sequence)
                .limit(limit)
            ).all()
        changes = [
            {
                "central_sequence": row.central_sequence,
                "event_id": row.event_id,
                "event_type": row.event_type,
                "aggregate_type": row.aggregate_type,
                "aggregate_id": row.aggregate_uuid,
                "aggregate_version": row.aggregate_version,
                "occurred_at": row.occurred_at,
                "payload": row.payload_json,
            }
            for row in rows
        ]
        return {
            "after": after,
            "next_checkpoint": (
                changes[-1]["central_sequence"] if changes else after
            ),
            "changes": changes,
        }

    @app.get(
        "/v1/cashier/service-invoice-drafts",
        dependencies=[Depends(cashier_identity)],
    )
    def list_ready_service_invoice_drafts(
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict:
        """Return immutable READY snapshots; opening one creates no server state."""
        with database_engine.connect() as connection:
            rows = connection.execute(
                select(invoice_drafts)
                .where(invoice_drafts.c.status == "READY")
                .order_by(invoice_drafts.c.created_at, invoice_drafts.c.draft_uuid)
                .limit(limit)
            ).all()
        return {
            "drafts": [
                {
                    "draft_id": row.draft_uuid,
                    "service_order_id": row.service_order_uuid,
                    "status": row.status,
                    "aggregate_version": 1,
                    "locked_payload": row.payload_json,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                }
                for row in rows
            ]
        }

    @app.get(
        "/v1/cashier/partners",
        dependencies=[Depends(cashier_identity)],
    )
    def search_billing_partners(
        q: str = Query(default="", max_length=120),
        limit: int = Query(default=30, ge=1, le=100),
    ) -> dict:
        query = select(partners).where(partners.c.active.is_(True))
        cleaned = q.strip()
        if cleaned:
            pattern = f"%{cleaned}%"
            query = query.where(or_(
                partners.c.name.ilike(pattern),
                partners.c.tax_number.ilike(pattern),
                partners.c.phone.ilike(pattern),
                partners.c.email.ilike(pattern),
            ))
        query = query.order_by(partners.c.name, partners.c.partner_uuid).limit(limit)
        with database_engine.connect() as connection:
            rows = connection.execute(query).all()
        return {
            "partners": [{
                "partner_id": row.partner_uuid,
                "row_version": row.row_version,
                "name": row.name,
                "address": row.address,
                "post_city": row.post_city,
                "country": row.country,
                "tax_number": row.tax_number,
                "registration_number": row.registration_number,
                "phone": row.phone,
                "email": row.email,
            } for row in rows]
        }

    @app.get(
        "/v1/cashier/partners/ajpes/{tax_number}",
        dependencies=[Depends(cashier_identity)],
    )
    def propose_partner_from_ajpes(tax_number: str) -> dict:
        """Return an uncommitted suggestion; CreatePartner is a separate command."""
        try:
            found = find_partner_by_tax_number(
                tax_number,
                test_mode=configured.environment != "production",
                timeout=15,
            )
        except AjpesError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "suggestion": {
                "verification_source": "AJPES_REVIEW",
                "review_confirmed": False,
                "name": found.name,
                "address": found.address,
                "post_city": found.post_city,
                "country": "Slovenija",
                "tax_number": found.tax_number,
                "registration_number": found.registration_number,
                "phone": "",
                "email": "",
                "source_reference": f"AJPES:{found.tax_number}",
            }
        }

    @app.get(
        "/v1/office/partners",
        dependencies=[Depends(office_identity)],
    )
    def search_office_partners(
        q: str = Query(default="", max_length=120),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict:
        query = select(partners).where(partners.c.active.is_(True))
        cleaned = q.strip()
        if cleaned:
            pattern = f"%{cleaned}%"
            query = query.where(or_(
                partners.c.name.ilike(pattern),
                partners.c.tax_number.ilike(pattern),
                partners.c.registration_number.ilike(pattern),
            ))
        with database_engine.connect() as connection:
            rows = connection.execute(
                query.order_by(partners.c.name, partners.c.partner_uuid).limit(limit)
            ).all()
        return {"partners": [{
            "partner_id": row.partner_uuid, "row_version": row.row_version,
            "name": row.name, "address": row.address,
            "post_city": row.post_city, "country": row.country,
            "tax_number": row.tax_number,
            "registration_number": row.registration_number,
            "phone": row.phone, "email": row.email,
        } for row in rows]}

    @app.get(
        "/v1/service/inventory-snapshot",
        dependencies=[Depends(service_identity)],
    )
    def complete_service_inventory_snapshot() -> dict:
        """Return one transactionally consistent and explicitly complete view."""
        with database_engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as connection:
            with connection.begin():
                snapshot_sequence = int(connection.execute(select(
                    func.coalesce(func.max(change_feed.c.central_sequence), 0)
                )).scalar_one())
                article_ids = connection.execute(select(
                    inventory_items.c.article_uuid
                ).order_by(inventory_items.c.article_uuid)).scalars().all()
                updates = _inventory_updates_for_articles(connection, article_ids)
        return {
            "complete": True,
            "snapshot_sequence": snapshot_sequence,
            "inventory_updates": updates,
        }

    @app.get(
        "/v1/office/inventory",
        dependencies=[Depends(office_identity)],
    )
    @app.get(
        "/v1/cashier/inventory",
        dependencies=[Depends(cashier_identity)],
    )
    def search_office_inventory(
        q: str = Query(default="", max_length=120),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict:
        joined = inventory_items.outerjoin(
            inventory_identities,
            inventory_identities.c.article_uuid == inventory_items.c.article_uuid,
        )
        identity_available = select(func.coalesce(
            func.sum(inventory_lots.c.available_quantity), 0
        )).where(
            inventory_lots.c.identity_uuid == inventory_identities.c.identity_uuid,
            inventory_lots.c.status == "AVAILABLE",
        ).correlate(inventory_identities).scalar_subquery()
        query = select(
            inventory_items.c.article_uuid,
            inventory_items.c.barcode.label("main_barcode"),
            inventory_items.c.description,
            inventory_items.c.quantity_on_hand,
            inventory_items.c.current_sale_price,
            inventory_items.c.row_version,
            inventory_items.c.blocked,
            inventory_items.c.blocked_reason,
            inventory_identities.c.identity_uuid,
            inventory_identities.c.supplier_barcode,
            identity_available.label("identity_quantity_available"),
        ).select_from(joined)
        cleaned = q.strip()
        if cleaned:
            pattern = f"%{cleaned}%"
            query = query.where(or_(
                inventory_items.c.description.ilike(pattern),
                inventory_items.c.barcode.ilike(pattern),
                inventory_identities.c.supplier_barcode.ilike(pattern),
            ))
        with database_engine.connect() as connection:
            rows = connection.execute(query.order_by(
                inventory_items.c.description,
                inventory_items.c.article_uuid,
                inventory_identities.c.supplier_barcode,
            ).limit(limit)).all()
        return {"inventory": [{
            "article_id": row.article_uuid, "row_version": row.row_version,
            "description": row.description, "main_barcode": row.main_barcode,
            "identity_id": row.identity_uuid,
            "supplier_barcode": row.supplier_barcode or "",
            "article_quantity_available": format(row.quantity_on_hand, "f"),
            "identity_quantity_available": format(
                row.identity_quantity_available, "f"
            ),
            "current_sale_price": format(row.current_sale_price, "f"),
            "blocked": row.blocked, "blocked_reason": row.blocked_reason or "",
        } for row in rows]}

    @app.get(
        "/v1/office/purchase-receipts",
        dependencies=[Depends(office_identity)],
    )
    def list_purchase_receipts(
        receipt_status: str = Query(default="", alias="status", max_length=30),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict:
        query = select(purchase_receipts)
        cleaned_status = receipt_status.strip().upper()
        if cleaned_status:
            if cleaned_status not in {"PENDING_INVOICE", "RELEASED"}:
                raise HTTPException(status_code=422, detail="Unsupported receipt status.")
            query = query.where(purchase_receipts.c.status == cleaned_status)
        with database_engine.connect() as connection:
            receipts = connection.execute(query.order_by(
                purchase_receipts.c.received_date.desc(),
                purchase_receipts.c.receipt_number.desc(),
            ).limit(limit)).all()
            result = []
            for receipt in receipts:
                lines = connection.execute(select(purchase_receipt_lines).where(
                    purchase_receipt_lines.c.receipt_uuid == receipt.receipt_uuid
                ).order_by(purchase_receipt_lines.c.line_no)).all()
                result.append({
                    "receipt_id": receipt.receipt_uuid,
                    "row_version": receipt.row_version,
                    "receipt_number": receipt.receipt_number,
                    "invoice_receipt_number": receipt.invoice_receipt_number or "",
                    "document_type": receipt.document_type,
                    "status": receipt.status,
                    "supplier": receipt.supplier_snapshot_json,
                    "external_document_no": receipt.external_document_no,
                    "invoice_external_no": receipt.invoice_external_no or "",
                    "document_date": receipt.document_date,
                    "received_date": receipt.received_date,
                    "document_total": format(receipt.document_total, "f"),
                    "shipping_total": format(receipt.shipping_total, "f"),
                    "lines": [{
                        "line_id": line.line_uuid, "line_no": line.line_no,
                        "article_id": line.article_uuid,
                        "quantity": format(line.quantity, "f"),
                        "supplier_barcode": line.supplier_barcode,
                        "lot_serial": line.lot_serial,
                        "landed_unit_cost": format(line.landed_unit_cost, "f"),
                        "confirmed_sale_price": format(line.confirmed_sale_price, "f"),
                    } for line in lines],
                })
        return {"receipts": result}

    @app.get(
        "/v1/office/receipt-document-types",
        dependencies=[Depends(office_identity)],
    )
    def list_receipt_document_types() -> dict:
        with database_engine.connect() as connection:
            rows = connection.execute(select(receipt_document_types).where(
                receipt_document_types.c.is_active.is_(True)
            ).order_by(receipt_document_types.c.type_code)).all()
        return {"custom_types": [{
            "type_code": row.type_code, "display_name": row.display_name,
        } for row in rows]}

    return app
