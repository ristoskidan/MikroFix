"""Dobaviteljski katalog MikroFIX z ločeno lokalno zalogo.

Modul bere samo javne produktne strani. Dobaviteljska cena in stanje nikoli ne
spreminjata obstoječih ponudb, dokumentov ali dejanske lokalne količine.
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from mikrofix_v29_core import recommend_price


MAX_PRODUCT_PAGE_BYTES = 3_000_000


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class _ProductMetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return
        values = {str(key).lower(): value or "" for key, value in attrs}
        key = values.get("property") or values.get("name")
        content = values.get("content", "").strip()
        if key and content:
            self.meta[key.lower()] = content


@dataclass(frozen=True)
class ProductPage:
    price: float
    currency: str
    availability: str
    sku: str
    product_name: str


def parse_fixshop_product_html(html: str) -> ProductPage:
    parser = _ProductMetaParser()
    parser.feed(html)
    raw_price = parser.meta.get("product:price:amount", "").replace(",", ".")
    try:
        price = float(raw_price)
    except ValueError as exc:
        raise ValueError("Na produktni strani manjka veljavna javna cena.") from exc
    if not 5.0 <= price <= 2_000.0:
        raise ValueError(f"Dobaviteljska cena {price:.2f} EUR je zunaj varnega obsega.")

    availability_match = re.search(
        r'"availability"\s*:\s*"([^"]+)"', html, flags=re.IGNORECASE
    )
    raw_availability = availability_match.group(1).lower() if availability_match else ""
    if "outofstock" in raw_availability:
        availability = "OUT_OF_STOCK"
    elif "instock" in raw_availability:
        availability = "IN_STOCK"
    elif "preorder" in raw_availability:
        availability = "PREORDER"
    else:
        availability = "UNKNOWN"

    return ProductPage(
        price=round(price, 2),
        currency=parser.meta.get("product:price:currency", "EUR") or "EUR",
        availability=availability,
        sku=parser.meta.get("product:retailer_item_id", ""),
        product_name=parser.meta.get("og:title", ""),
    )


class SupplierCatalogStore:
    def __init__(
        self,
        db_path: Path,
        pricing_settings: dict[str, Any],
        supplier_settings: dict[str, Any],
    ) -> None:
        self.db_path = Path(db_path)
        self.pricing = pricing_settings
        self.settings = supplier_settings

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def init(self) -> None:
        connection = self.connect()
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS supplier_catalog(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier TEXT NOT NULL,
                model TEXT NOT NULL,
                repair_type TEXT NOT NULL,
                repair_label TEXT NOT NULL,
                quality TEXT NOT NULL,
                product_name TEXT NOT NULL,
                source_url TEXT NOT NULL UNIQUE,
                supplier_sku TEXT,
                public_price REAL,
                currency TEXT NOT NULL DEFAULT 'EUR',
                availability TEXT NOT NULL DEFAULT 'UNKNOWN',
                competitor_price REAL,
                auto_eligible INTEGER NOT NULL DEFAULT 0,
                inventory_tier TEXT NOT NULL DEFAULT 'C',
                target_vehicle INTEGER NOT NULL DEFAULT 0,
                target_home INTEGER NOT NULL DEFAULT 0,
                reorder_point INTEGER NOT NULL DEFAULT 0,
                local_inventory_id INTEGER,
                part_sale_price REAL,
                landed_cost REAL,
                pickup_price REAL,
                onsite_price REAL,
                gross_margin REAL,
                pricing_mode TEXT,
                offer_status TEXT NOT NULL DEFAULT 'PO PONUDBI',
                etag TEXT,
                last_modified TEXT,
                last_checked TEXT,
                last_success TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(local_inventory_id) REFERENCES inventory_items(id)
                    ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_supplier_catalog_model
                ON supplier_catalog(model COLLATE NOCASE, repair_type);
            CREATE INDEX IF NOT EXISTS idx_supplier_catalog_local
                ON supplier_catalog(local_inventory_id);
            CREATE TABLE IF NOT EXISTS supplier_refresh_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                forced INTEGER NOT NULL DEFAULT 0,
                requested INTEGER NOT NULL DEFAULT 0,
                succeeded INTEGER NOT NULL DEFAULT 0,
                unchanged INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                details TEXT
            );
            """
        )
        connection.commit()
        connection.close()

    def seed_from_file(self, path: Path) -> int:
        path = Path(path)
        if not path.exists():
            return 0
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schemaVersion") != 1 or not isinstance(payload.get("products"), list):
            raise ValueError("Nepodprta začetna dobaviteljska datoteka.")
        supplier = str(payload.get("supplier") or "FixShop")
        captured = str(payload.get("generatedAt") or iso_now())
        now = iso_now()
        connection = self.connect()
        for product in payload["products"]:
            connection.execute(
                """
                INSERT INTO supplier_catalog(
                    supplier, model, repair_type, repair_label, quality,
                    product_name, source_url, public_price, currency,
                    availability, competitor_price, auto_eligible,
                    inventory_tier, target_vehicle, target_home, reorder_point,
                    last_checked, last_success, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_url) DO UPDATE SET
                    supplier=excluded.supplier,
                    model=excluded.model,
                    repair_type=excluded.repair_type,
                    repair_label=excluded.repair_label,
                    quality=excluded.quality,
                    product_name=excluded.product_name,
                    public_price=CASE
                        WHEN supplier_catalog.last_success IS NULL
                        THEN excluded.public_price ELSE supplier_catalog.public_price END,
                    competitor_price=excluded.competitor_price,
                    auto_eligible=excluded.auto_eligible,
                    inventory_tier=excluded.inventory_tier,
                    target_vehicle=excluded.target_vehicle,
                    target_home=excluded.target_home,
                    reorder_point=excluded.reorder_point,
                    updated_at=excluded.updated_at
                """,
                (
                    supplier,
                    str(product["model"]),
                    str(product["repairType"]),
                    str(product["repairLabel"]),
                    str(product["quality"]),
                    str(product["productName"]),
                    str(product["sourceUrl"]),
                    float(product["publicPrice"]),
                    "EUR",
                    "UNKNOWN",
                    product.get("competitorPrice"),
                    int(bool(product.get("autoEligible"))),
                    str(product.get("inventoryTier") or "C"),
                    int(product.get("targetVehicle") or 0),
                    int(product.get("targetHome") or 0),
                    int(product.get("reorderPoint") or 0),
                    captured,
                    captured,
                    now,
                    now,
                ),
            )
        connection.commit()
        self._recalculate_all(connection)
        self._relink_inventory(connection)
        connection.commit()
        count = connection.execute("SELECT COUNT(*) FROM supplier_catalog").fetchone()[0]
        connection.close()
        return int(count)

    def _calculate_offer(
        self,
        public_price: float,
        category: str,
        repair_type: str,
        competitor_price: float | None,
        auto_eligible: bool,
    ) -> dict[str, Any]:
        rules = self.pricing.get("categories", {})
        if category not in rules:
            raise ValueError(f"Manjka maržno pravilo za kategorijo {category}.")
        rule = rules[category]
        recommendation = recommend_price(public_price, rule, profile_code=category)
        landed = recommendation.landed_cost
        target = recommendation.recommended_price
        t1 = float(self.pricing.get("t1_hour_eur", 30.0))
        pickup_return = float(self.pricing.get("pickup_return_eur", 20.0))
        onsite_first = float(self.pricing.get("onsite_first_hour_eur", 90.0))
        premiums = self.pricing.get("competitive_premiums", {})
        if repair_type == "screen":
            pickup_premium = float(premiums.get("screen_pickup_eur", 35.0))
            onsite_premium = float(premiums.get("screen_onsite_eur", 80.0))
        else:
            pickup_premium = float(premiums.get("battery_pickup_eur", 15.0))
            onsite_premium = float(premiums.get("battery_onsite_eur", 60.0))

        def competitive(part_price: float) -> bool:
            if competitor_price is None:
                return True
            return (
                part_price + t1 + pickup_return <= float(competitor_price) + pickup_premium
                and part_price + onsite_first <= float(competitor_price) + onsite_premium
            )

        selected = target
        pickup = selected + t1 + pickup_return
        onsite = selected + onsite_first
        contribution = selected - landed
        margin = contribution / selected if selected else 0.0
        passes = (
            auto_eligible
            and selected + 1e-9 >= float(public_price) * 1.05
            and competitive(selected)
        )
        return {
            "part_sale_price": selected,
            "landed_cost": round(landed, 2),
            "pickup_price": round(pickup, 2),
            "onsite_price": round(onsite, 2),
            "gross_margin": round(margin, 4),
            "pricing_mode": "TIERED_MARGIN_V1",
            "offer_status": "OBJAVI" if passes else "PO PONUDBI",
        }

    def _recalculate_row(self, connection: sqlite3.Connection, row: sqlite3.Row) -> None:
        if row["public_price"] is None:
            return
        calculated = self._calculate_offer(
            float(row["public_price"]),
            str(row["quality"]),
            str(row["repair_type"]),
            float(row["competitor_price"]) if row["competitor_price"] is not None else None,
            bool(row["auto_eligible"]),
        )
        connection.execute(
            """
            UPDATE supplier_catalog SET
                part_sale_price=?, landed_cost=?, pickup_price=?, onsite_price=?,
                gross_margin=?, pricing_mode=?, offer_status=?, updated_at=?
            WHERE id=?
            """,
            (
                calculated["part_sale_price"],
                calculated["landed_cost"],
                calculated["pickup_price"],
                calculated["onsite_price"],
                calculated["gross_margin"],
                calculated["pricing_mode"],
                calculated["offer_status"],
                iso_now(),
                row["id"],
            ),
        )

    def _recalculate_all(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute("SELECT * FROM supplier_catalog ORDER BY id").fetchall()
        for row in rows:
            self._recalculate_row(connection, row)

    def _relink_inventory(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            UPDATE supplier_catalog
            SET local_inventory_id=(
                SELECT id FROM inventory_items
                WHERE article_name=supplier_catalog.product_name COLLATE NOCASE
                ORDER BY id LIMIT 1
            )
            WHERE local_inventory_id IS NULL
            """
        )

    def relink_inventory(self) -> None:
        connection = self.connect()
        self._relink_inventory(connection)
        connection.commit()
        connection.close()

    def ensure_inventory_item(self, catalog_id: int) -> int:
        connection = self.connect()
        row = connection.execute(
            "SELECT * FROM supplier_catalog WHERE id=?", (int(catalog_id),)
        ).fetchone()
        if row is None:
            connection.close()
            raise ValueError("Dobaviteljski artikel ne obstaja.")
        if row["local_inventory_id"] is not None:
            inventory_id = int(row["local_inventory_id"])
            connection.close()
            return inventory_id
        existing = connection.execute(
            "SELECT id FROM inventory_items WHERE article_name=? COLLATE NOCASE LIMIT 1",
            (row["product_name"],),
        ).fetchone()
        if existing:
            inventory_id = int(existing[0])
        else:
            barcode = f"MF-FS-{int(row['id']):05d}"
            internal_code = str(row["supplier_sku"] or f"FS-{int(row['id']):05d}")
            now = iso_now()
            cursor = connection.execute(
                """
                INSERT INTO inventory_items(
                    barcode, article_name, internal_code, stock_qty, price,
                    lot_serial, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    barcode,
                    row["product_name"],
                    internal_code,
                    0,
                    float(row["part_sale_price"] or 0.0),
                    "",
                    now,
                    now,
                ),
            )
            inventory_id = int(cursor.lastrowid)
        connection.execute(
            "UPDATE supplier_catalog SET local_inventory_id=?, updated_at=? WHERE id=?",
            (inventory_id, iso_now(), int(catalog_id)),
        )
        connection.commit()
        connection.close()
        return inventory_id

    def _fetch_one(self, row: dict[str, Any]) -> dict[str, Any]:
        source_url = str(row["source_url"])
        parsed_url = urllib.parse.urlparse(source_url)
        allowed_hosts = {
            str(host).strip().lower()
            for host in self.settings.get("allowed_hosts", ["fixshop.si"])
            if str(host).strip()
        }
        if parsed_url.scheme.lower() != "https" or (
            parsed_url.hostname or ""
        ).lower() not in allowed_hosts:
            raise ValueError("Dobaviteljski naslov ni na dovoljenem HTTPS gostitelju.")
        headers = {
            "User-Agent": str(self.settings.get("user_agent") or "MikroFIX/28 TEST"),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "sl,en;q=0.8",
        }
        if row.get("etag"):
            headers["If-None-Match"] = str(row["etag"])
        if row.get("last_modified"):
            headers["If-Modified-Since"] = str(row["last_modified"])
        request = urllib.request.Request(source_url, headers=headers)
        try:
            with urllib.request.urlopen(
                request, timeout=float(self.settings.get("timeout_seconds", 12))
            ) as response:
                body = response.read(MAX_PRODUCT_PAGE_BYTES + 1)
                if len(body) > MAX_PRODUCT_PAGE_BYTES:
                    raise ValueError("Produktna stran je nepričakovano velika.")
                charset = response.headers.get_content_charset() or "utf-8"
                parsed = parse_fixshop_product_html(body.decode(charset, errors="replace"))
                return {
                    "id": row["id"],
                    "status": "success",
                    "page": parsed,
                    "etag": response.headers.get("ETag"),
                    "last_modified": response.headers.get("Last-Modified"),
                }
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return {"id": row["id"], "status": "unchanged"}
            raise

    def refresh_due(
        self,
        *,
        force: bool = False,
        progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        if not bool(self.settings.get("enabled", True)):
            return {"status": "disabled", "requested": 0, "succeeded": 0, "unchanged": 0, "failed": 0}
        refresh_hours = int(self.settings.get("refresh_hours", 24))
        cutoff = utc_now() - timedelta(hours=refresh_hours)
        connection = self.connect()
        all_rows = [dict(row) for row in connection.execute(
            "SELECT id, source_url, etag, last_modified, last_success FROM supplier_catalog ORDER BY id"
        ).fetchall()]
        due = all_rows if force else [
            row for row in all_rows
            if parse_iso(row.get("last_success")) is None
            or parse_iso(row.get("last_success")) < cutoff
        ]
        if not due:
            connection.close()
            return {"status": "fresh", "requested": 0, "succeeded": 0, "unchanged": 0, "failed": 0}

        started = iso_now()
        cursor = connection.execute(
            "INSERT INTO supplier_refresh_log(supplier,started_at,forced,requested) VALUES(?,?,?,?)",
            ("FixShop", started, int(force), len(due)),
        )
        log_id = int(cursor.lastrowid)
        connection.commit()

        results: list[dict[str, Any]] = []
        completed = 0
        workers = max(1, min(4, int(self.settings.get("max_workers", 2))))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fixshop") as pool:
            futures = {pool.submit(self._fetch_one, row): row for row in due}
            for future in as_completed(futures):
                source = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:  # posamezna napaka ne ustavi ostalih cen
                    results.append({"id": source["id"], "status": "failed", "error": str(exc)[:500]})
                completed += 1
                if progress:
                    progress(completed, len(due))

        succeeded = unchanged = failed = 0
        checked = iso_now()
        for result in results:
            if result["status"] == "success":
                page: ProductPage = result["page"]
                connection.execute(
                    """
                    UPDATE supplier_catalog SET
                        public_price=?, currency=?, availability=?, supplier_sku=COALESCE(NULLIF(?,''),supplier_sku),
                        product_name=COALESCE(NULLIF(?,''),product_name), etag=?, last_modified=?,
                        last_checked=?, last_success=?, last_error=NULL, updated_at=?
                    WHERE id=?
                    """,
                    (
                        page.price,
                        page.currency,
                        page.availability,
                        page.sku,
                        page.product_name,
                        result.get("etag"),
                        result.get("last_modified"),
                        checked,
                        checked,
                        checked,
                        result["id"],
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM supplier_catalog WHERE id=?", (result["id"],)
                ).fetchone()
                self._recalculate_row(connection, updated)
                succeeded += 1
            elif result["status"] == "unchanged":
                connection.execute(
                    "UPDATE supplier_catalog SET last_checked=?,last_success=?,last_error=NULL,updated_at=? WHERE id=?",
                    (checked, checked, checked, result["id"]),
                )
                unchanged += 1
            else:
                connection.execute(
                    "UPDATE supplier_catalog SET last_checked=?,last_error=?,updated_at=? WHERE id=?",
                    (checked, result.get("error", "Neznana napaka"), checked, result["id"]),
                )
                failed += 1
        self._relink_inventory(connection)
        details = json.dumps({"succeeded": succeeded, "unchanged": unchanged, "failed": failed}, ensure_ascii=False)
        connection.execute(
            """
            UPDATE supplier_refresh_log SET finished_at=?,succeeded=?,unchanged=?,failed=?,details=?
            WHERE id=?
            """,
            (checked, succeeded, unchanged, failed, details, log_id),
        )
        connection.commit()
        connection.close()
        return {
            "status": "completed",
            "requested": len(due),
            "succeeded": succeeded,
            "unchanged": unchanged,
            "failed": failed,
        }

    def list_catalog(self, search: str = "") -> list[dict[str, Any]]:
        connection = self.connect()
        self._relink_inventory(connection)
        connection.commit()
        sql = """
            SELECT c.*, i.stock_qty AS local_stock_qty
            FROM supplier_catalog c
            LEFT JOIN inventory_items i ON i.id=c.local_inventory_id
        """
        params: list[Any] = []
        if search.strip():
            sql += " WHERE c.model LIKE ? OR c.product_name LIKE ? OR c.quality LIKE ?"
            value = f"%{search.strip()}%"
            params.extend([value, value, value])
        sql += " ORDER BY c.model COLLATE NOCASE, c.repair_type DESC"
        rows = [dict(row) for row in connection.execute(sql, params).fetchall()]
        connection.close()
        refresh_hours = int(self.settings.get("refresh_hours", 24))
        cutoff = utc_now() - timedelta(hours=refresh_hours)
        for row in rows:
            if row["local_inventory_id"] is None:
                row["local_status"] = "NI V LOKALNI BAZI"
            elif int(row["local_stock_qty"] or 0) <= 0:
                row["local_status"] = "NI NA LOKALNI ZALOGI"
            else:
                row["local_status"] = f"NA ZALOGI: {int(row['local_stock_qty'])}"
            success = parse_iso(row.get("last_success"))
            stale = success is None or success < cutoff
            if row.get("last_error"):
                row["supplier_status"] = "ZADNJA DOBRA CENA • NAPAKA"
            elif stale:
                row["supplier_status"] = "ZASTARELA CENA"
            elif row.get("availability") == "IN_STOCK":
                row["supplier_status"] = "NA ZALOGI PRI DOBAVITELJU"
            elif row.get("availability") == "OUT_OF_STOCK":
                row["supplier_status"] = "NI PRI DOBAVITELJU"
            else:
                row["supplier_status"] = "CENA PREVERJENA • ZALOGA NEZNANA"
        return rows

    def summary(self) -> dict[str, int]:
        rows = self.list_catalog()
        return {
            "items": len(rows),
            "publishable": sum(row["offer_status"] == "OBJAVI" for row in rows),
            "local_available": sum(str(row["local_status"]).startswith("NA ZALOGI:") for row in rows),
            "errors": sum(bool(row.get("last_error")) for row in rows),
        }

    def export_site_prices(self, path: Path) -> dict[str, Any]:
        """Izvozi javni cenik brez dobaviteljskih cen in nabavnih povezav."""
        rows = self.list_catalog()
        by_model: dict[str, dict[str, dict[str, Any]]] = {}
        model_order: list[str] = []
        for row in rows:
            model = str(row["model"])
            if model not in by_model:
                by_model[model] = {}
                model_order.append(model)
            repair_type = str(row["repair_type"])
            current = by_model[model].get(repair_type)
            if current is None or (
                row["offer_status"] == "OBJAVI"
                and current["offer_status"] != "OBJAVI"
            ):
                by_model[model][repair_type] = row
        model_order.sort(
            key=lambda model: min(
                int(row["id"]) for row in by_model[model].values()
            )
        )

        models: list[dict[str, Any]] = []
        t1 = float(self.pricing.get("t1_hour_eur", 30.0))
        pickup_return = float(self.pricing.get("pickup_return_eur", 20.0))
        onsite_first = float(self.pricing.get("onsite_first_hour_eur", 90.0))
        bundle_discount = float(self.pricing.get("bundle_discount_eur", 5.0))

        def public_service(repair_type: str, row: dict[str, Any] | None) -> dict[str, Any]:
            label = "Menjava zaslona" if repair_type == "screen" else "Menjava baterije"
            if row is None:
                return {
                    "id": repair_type,
                    "label": label,
                    "status": "quote",
                    "pickup": None,
                    "onsite": None,
                    "quality": "Po potrditvi ustreznega dela",
                    "localStock": 0,
                    "note": "Cena in dobavljivost se potrdita pred naročilom.",
                }
            local_stock = max(0, int(row.get("local_stock_qty") or 0))
            fixed = row["offer_status"] == "OBJAVI"
            if not fixed:
                note = "Izbrani kakovostni del trenutno nima varne fiksne javne cene."
            elif local_stock:
                note = f"Na lokalni zalogi MikroFIX: {local_stock} kos."
            else:
                note = (
                    "Del trenutno ni na lokalni zalogi MikroFIX. "
                    "Dobavljivost se potrdi ob naročilu."
                )
            return {
                "id": repair_type,
                "label": label,
                "status": "published" if fixed else "quote",
                "pickup": round(float(row["pickup_price"]), 2) if fixed else None,
                "onsite": round(float(row["onsite_price"]), 2) if fixed else None,
                "quality": str(row["quality"]),
                "localStock": local_stock,
                "note": note,
            }

        for model in model_order:
            selected = by_model[model]
            screen_row = selected.get("screen")
            battery_row = selected.get("battery")
            screen = public_service("screen", screen_row)
            battery = public_service("battery", battery_row)
            combo = None
            if screen["status"] == "published" and battery["status"] == "published":
                screen_part = float(screen_row["part_sale_price"])
                battery_part = float(battery_row["part_sale_price"])
                pickup_combo = screen_part + battery_part + t1 + pickup_return - bundle_discount
                onsite_combo = screen_part + battery_part + onsite_first - bundle_discount
                combo = {
                    "pickup": round(pickup_combo, 2),
                    "onsite": round(onsite_combo, 2),
                    "pickupSaving": round(
                        float(screen["pickup"]) + float(battery["pickup"]) - pickup_combo,
                        2,
                    ),
                    "onsiteSaving": round(
                        float(screen["onsite"]) + float(battery["onsite"]) - onsite_combo,
                        2,
                    ),
                }
            models.append({
                "model": model,
                "screenLabel": str(screen_row["quality"]) if screen_row else "Po ponudbi",
                "services": [
                    screen,
                    battery,
                    {
                        "id": "other",
                        "label": "Drugo popravilo / diagnostika",
                        "status": "quote",
                        "pickup": None,
                        "onsite": None,
                        "quality": "Po pregledu",
                        "localStock": 0,
                        "note": (
                            "Poseg in morebitni deli se določijo po opisu napake "
                            "oziroma diagnostiki."
                        ),
                    },
                ],
                "combo": combo,
            })

        payload = {
            "schemaVersion": 1,
            "validFrom": datetime.now().astimezone().date().isoformat(),
            "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "currency": "EUR",
            "priceType": "informative_offer",
            "refreshPolicy": {
                "supplier": "FixShop public prices",
                "maxAgeHours": int(self.settings.get("refresh_hours", 24)),
                "localStockIsSeparate": True,
            },
            "fulfillment": {
                "organizedPickupReturn": {
                    "price": round(pickup_return, 2),
                    "maxExtraKmTotal": int(
                        self.pricing.get("pickup_return_max_extra_km_total", 12)
                    ),
                    "note": (
                        "Cena velja za organizirani prevzem in vračilo na dnevni trasi."
                    ),
                },
                "dedicatedPickupReturn": {
                    "price": round(
                        float(self.pricing.get("dedicated_pickup_return_eur", 30.0)),
                        2,
                    ),
                    "status": "quote",
                    "note": (
                        "Namenski prevzem se potrdi glede na naslov in dejanski odklon."
                    ),
                },
            },
            "models": models,
        }
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return payload
