from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import unicodedata
from difflib import SequenceMatcher
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import pandas as pd
import pdfplumber
import streamlit as st
from sqlalchemy import (
    Column, Date as SQLDate, DateTime, Float, ForeignKey, Index, Integer,
    MetaData, String, Table, Text, UniqueConstraint, create_engine, delete,
    insert, select, text as sql_text, update,
)


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "fillrate.db"
BUSINESS_LINES = ["Karay", "Lácteos El Pino"]

metadata = MetaData()
cargas_table = Table(
    "cargas", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("fecha", DateTime, nullable=False),
    Column("cliente", String(200), nullable=False, default=""),
    Column("linea_negocio", String(80), nullable=False, default="Karay"),
    Column("usuario", String(120), nullable=False, default=""),
    Column("fill_rate", Float, nullable=False),
    Column("venta_potencial", Float, nullable=False),
    Column("venta_facturada", Float, nullable=False),
    Column("venta_perdida", Float, nullable=False),
    Column("unidades_pedidas", Float, nullable=False),
    Column("unidades_facturadas", Float, nullable=False),
    Column("pedidos_archivos", Integer, nullable=False),
    Column("facturas_archivos", Integer, nullable=False),
    Column("nombres_pedidos", Text, nullable=False, default="[]"),
    Column("nombres_facturas", Text, nullable=False, default="[]"),
    Column("fingerprint", String(64), nullable=False, unique=True),
)
detalle_table = Table(
    "detalle_fillrate", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("carga_id", Integer, ForeignKey("cargas.id", ondelete="CASCADE"), nullable=False),
    Column("oc", String(40), nullable=False, default=""),
    Column("codigo", String(40), nullable=False, default=""),
    Column("producto", Text, nullable=False, default=""),
    Column("pedidas", Float, nullable=False),
    Column("facturadas", Float, nullable=False),
    Column("pendientes", Float, nullable=False),
    Column("precio_unitario", Float, nullable=False),
    Column("venta_potencial", Float, nullable=False),
    Column("venta_facturada", Float, nullable=False),
    Column("venta_perdida", Float, nullable=False),
    Column("fill_rate", Float, nullable=False),
)
Index("idx_detalle_carga", detalle_table.c.carga_id)
Index("idx_detalle_oc", detalle_table.c.oc)
Index("idx_cargas_fecha", cargas_table.c.fecha)

forecast_table = Table(
    "forecast", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("periodo", String(80), nullable=False, unique=True),
    Column("archivo", String(255), nullable=False, default=""),
    Column("actualizado", DateTime, nullable=False),
)
forecast_detalle_table = Table(
    "forecast_detalle", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("forecast_id", Integer, ForeignKey("forecast.id", ondelete="CASCADE"), nullable=False),
    Column("codigo_interno", String(20), nullable=False),
    Column("ean", String(20), nullable=False, default=""),
    Column("producto", Text, nullable=False, default=""),
    Column("unidades", Float, nullable=False),
    UniqueConstraint("forecast_id", "codigo_interno", name="uq_forecast_producto"),
)
produccion_table = Table(
    "produccion", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("periodo", String(80), nullable=False, unique=True),
    Column("archivo", String(255), nullable=False, default=""),
    Column("actualizado", DateTime, nullable=False),
)
produccion_detalle_table = Table(
    "produccion_detalle", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("produccion_id", Integer, ForeignKey("produccion.id", ondelete="CASCADE"), nullable=False),
    Column("codigo_interno", String(20), nullable=False),
    Column("ean", String(20), nullable=False, default=""),
    Column("producto", Text, nullable=False, default=""),
    Column("unidades", Float, nullable=False),
    UniqueConstraint("produccion_id", "codigo_interno", name="uq_produccion_producto"),
)
facturas_acumuladas_table = Table(
    "facturas_acumuladas", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("archivo_hash", String(64), nullable=False),
    Column("archivo", String(255), nullable=False),
    Column("linea_negocio", String(80), nullable=False, default="Karay"),
    Column("linea", Integer, nullable=False),
    Column("fecha_documento", SQLDate, nullable=False),
    Column("codigo_interno", String(20), nullable=False, default=""),
    Column("ean", String(20), nullable=False, default=""),
    Column("producto", Text, nullable=False, default=""),
    Column("unidades", Float, nullable=False),
    UniqueConstraint("archivo_hash", "linea", name="uq_factura_archivo_linea"),
)
Index("idx_forecast_periodo", forecast_table.c.periodo)
Index("idx_produccion_periodo", produccion_table.c.periodo)
Index("idx_facturas_fecha", facturas_acumuladas_table.c.fecha_documento)
Index("idx_facturas_codigo", facturas_acumuladas_table.c.codigo_interno)

DETAIL_COLUMNS = [
    "oc", "codigo", "producto", "pedidas", "facturadas", "pendientes",
    "precio_unitario", "venta_potencial", "venta_facturada", "venta_perdida", "fill_rate",
]


def secret_value(key: str, default=None):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


@st.cache_resource
def get_engine():
    database_url = os.getenv("DATABASE_URL") or secret_value("database_url")
    if database_url:
        # Some providers still show the legacy postgres:// prefix.
        database_url = str(database_url).replace("postgres://", "postgresql+psycopg://", 1)
        if database_url.startswith("postgresql://"):
            database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        return create_engine(database_url, pool_pre_ping=True)
    return create_engine(f"sqlite:///{DB_PATH}")


def init_database() -> None:
    metadata.create_all(get_engine())
    # Forward-compatible migration for databases created before business lines.
    # Existing information always belongs to the original Karay operation.
    engine = get_engine()
    with engine.begin() as db:
        if engine.dialect.name == "postgresql":
            db.execute(sql_text(
                "ALTER TABLE cargas ADD COLUMN IF NOT EXISTS "
                "linea_negocio VARCHAR(80) NOT NULL DEFAULT 'Karay'"
            ))
            db.execute(sql_text(
                "ALTER TABLE facturas_acumuladas ADD COLUMN IF NOT EXISTS "
                "linea_negocio VARCHAR(80) NOT NULL DEFAULT 'Karay'"
            ))
            db.execute(sql_text("ALTER TABLE forecast ALTER COLUMN periodo TYPE VARCHAR(80)"))
            db.execute(sql_text("ALTER TABLE produccion ALTER COLUMN periodo TYPE VARCHAR(80)"))
        elif engine.dialect.name == "sqlite":
            carga_columns = {row[1] for row in db.execute(sql_text("PRAGMA table_info(cargas)"))}
            if "linea_negocio" not in carga_columns:
                db.execute(sql_text(
                    "ALTER TABLE cargas ADD COLUMN linea_negocio VARCHAR(80) NOT NULL DEFAULT 'Karay'"
                ))
            ledger_columns = {
                row[1] for row in db.execute(sql_text("PRAGMA table_info(facturas_acumuladas)"))
            }
            if "linea_negocio" not in ledger_columns:
                db.execute(sql_text(
                    "ALTER TABLE facturas_acumuladas ADD COLUMN "
                    "linea_negocio VARCHAR(80) NOT NULL DEFAULT 'Karay'"
                ))
        db.execute(sql_text(
            "UPDATE forecast SET periodo = periodo || '|Karay' "
            "WHERE periodo NOT LIKE '%|%'"
        ))
        db.execute(sql_text(
            "UPDATE produccion SET periodo = periodo || '|Karay' "
            "WHERE periodo NOT LIKE '%|%'"
        ))


def period_key(period: str, business_line: str) -> str:
    return f"{period}|{business_line}"


def period_from_key(value: str) -> str:
    return str(value).split("|", 1)[0]


def line_from_period_key(value: str) -> str:
    parts = str(value).split("|", 1)
    return parts[1] if len(parts) == 2 else "Karay"


def normalize_number(value: object) -> float:
    text = str(value or "").strip().replace("$", "").replace(" ", "")
    if not text:
        return 0.0
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        # Ecuadorian invoices use a comma with 4-5 decimal places (132,0000).
        # A lone comma is therefore decimal, not a thousands separator.
        text = text.replace(",", ".")
    try:
        return float(re.sub(r"[^0-9.\-]", "", text))
    except ValueError:
        return 0.0


def clean_code(value: object) -> str:
    match = re.search(r"\d{6,14}", str(value or "").replace(" ", ""))
    return match.group(0) if match else str(value or "").strip()


def extract_pdf(uploaded_file) -> tuple[str, list[list[list[str | None]]]]:
    uploaded_file.seek(0)
    with pdfplumber.open(io.BytesIO(uploaded_file.read())) as pdf:
        texts, tables = [], []
        for page in pdf.pages:
            texts.append(page.extract_text(x_tolerance=2, y_tolerance=3) or "")
            tables.extend(page.extract_tables() or [])
    uploaded_file.seek(0)
    # Form-feed keeps page boundaries. It is harmless whitespace for the
    # existing readers and lets a multi-invoice PDF be separated correctly.
    return "\n\f\n".join(texts), tables


def find_context(text: str) -> tuple[str, str]:
    oc_patterns = [r"(?:orden\s+de\s+compra|orden|pedido|o\.?c\.?)\s*[:#Nº°-]*\s*((?:\d[ ]*){6,20})"]
    client_patterns = [r"(?:cliente|raz[oó]n\s+social)\s*[:\-]\s*([^\n]{3,80})"]
    oc = next((re.sub(r"\D", "", m.group(1)) for p in oc_patterns if (m := re.search(p, text, re.I))), "")
    cliente = next((m.group(1).strip() for p in client_patterns if (m := re.search(p, text, re.I))), "")
    if not cliente and re.search(r"corporaci[oó]n\s+favorita|supermaxi", text, re.I):
        cliente = "Corporación Favorita"
    return oc, cliente


def invoice_oc(text: str) -> str:
    """Return numeric or alphanumeric OC printed in an invoice footer."""
    explicit = re.search(
        # El Rosado prints the destination between OC and the number, for
        # example "OC GYE: 5401477656" or "OC UIO: 5401477656".
        r"\bOC(?:\s+(?:GYE|UIO))?\s*:\s*((?:[A-Z]{1,3}\s*)?\d(?:[A-Z0-9 ]{3,18}\d))",
        text,
        re.I,
    )
    if explicit:
        return re.sub(r"\s+", "", explicit.group(1)).upper()
    # Casa Deli uses the Odoo sales-order origin as its order reference.
    if re.search(r"CASA DELI", text, re.I):
        origin = re.search(r"\bOrigen\s*:\s*([A-Z]\d{4,})", text, re.I)
        if origin:
            return origin.group(1).upper()
    return find_context(text)[0]


def known_customer(text: str) -> str:
    return next((
        name for marker, name in (
            (r"CORPORACION FAVORITA|SUPERMAXI", "Corporación Favorita"),
            (r"CASA DELI", "Casa Deli"),
            (r"GERARDO ORTIZ E HIJOS", "Coral / Gerardo Ortiz"),
            (r"TIENDAS INDUSTRIAL(?:ES)? ASOCIADAS|\bTIA S\.A", "Tía"),
            (r"CORPORACION EL ROSADO", "Corporación El Rosado"),
        ) if re.search(marker, text, re.I)
    ), "")


def excluded_product(row: dict | pd.Series) -> bool:
    """Products discontinued by the business and excluded from every KPI."""
    product = re.sub(r"\s+", " ", str(row.get("producto", ""))).upper()
    codes = {
        re.sub(r"\D", "", str(row.get("codigo", ""))),
        re.sub(r"\D", "", str(row.get("codigo_interno", ""))),
    }
    # Aceite de Coco 270 ml Sunshine (El Rosado).  The customer order can
    # expose either its internal reference or its article number.
    return (
        "SUNSHINE" in product
        or bool(codes & {"1140001", "01140001", "40633856", "000000000040633856"})
    )


def favorita_order_rows(text: str) -> list[dict]:
    """Read Corporacion Favorita orders: Pedida is cases and UC is units/case."""
    if "CORPORACION FAVORITA" not in text.upper() or "ORDEN COMPRA" not in text.upper():
        return []
    section_pattern = re.compile(
        r"ORDEN COMPRA[^\n:]*:\s*((?:\d[ ]*){8,20})(.*?)(?=ORDEN COMPRA[^\n:]*:|\Z)",
        re.I | re.S,
    )
    line_pattern = re.compile(
        r"^\d{2}(?P<product>.+?)\s+(?P<internal>\d{6})\s+\*?\s*"
        r"(?P<ean>\d{13})\s+(?P<uc>\d+)\s+(?P<price>\d+[.,]\d{4})"
        r"(?:\s+\d+[.,]\d+)*\s+(?P<cases>\d+)\s*$",
        re.M,
    )
    rows = []
    for section in section_pattern.finditer(text):
        oc = re.sub(r"\D", "", section.group(1))
        for match in line_pattern.finditer(section.group(2)):
            units_per_case = normalize_number(match.group("uc"))
            cases = normalize_number(match.group("cases"))
            rows.append({
                "oc": oc,
                "codigo_interno": "",
                "codigo": match.group("ean"),
                "producto": re.sub(r"\s+", " ", match.group("product")).strip(),
                "cantidad": cases * units_per_case,
                "precio": normalize_number(match.group("price")),
                "tipo": "pedido",
            })
    return rows


def odoo_order_rows(text: str) -> list[dict]:
    """Read customer sales orders exported from Odoo (for example S04899)."""
    if "FECHA DE PEDIDO" not in text.upper() or "DESCRIPCIÓN CANTIDAD" not in text.upper():
        return []
    oc_match = re.search(r"Orden\s*#\s*([A-Z0-9-]+)", text, re.I)
    oc = oc_match.group(1) if oc_match else ""
    row_pattern = re.compile(
        r"^\[(?P<internal>[A-Z0-9]+)\]\s+(?P<body>.*?)"
        r"(?=^\[[A-Z0-9]+\]|^Base imponible|\Z)", re.M | re.S,
    )
    qty_price = re.compile(r"(?P<qty>(?:\d{1,3}(?:\.\d{3})+|\d+),\d{4})\s+(?:Unidades\s+)?(?P<price>\d+[.,]\d{5})")
    rows = []
    for match in row_pattern.finditer(text):
        internal = match.group("internal")
        if not internal.isdigit():
            continue
        body = re.sub(r"\s+", " ", match.group("body")).strip()
        amounts = qty_price.search(body)
        if not amounts:
            continue
        eans = re.findall(r"(?<!\d)(\d{12,13})(?!\d)", body)
        description = body[:amounts.start()].strip(" -")
        rows.append({
            "oc": oc,
            "codigo_interno": internal.zfill(8),
            "codigo": eans[-1] if eans else internal.zfill(8),
            "producto": description,
            "cantidad": normalize_number(amounts.group("qty")),
            "precio": normalize_number(amounts.group("price")),
            "tipo": "pedido",
        })
    return rows


def coral_order_rows(text: str, tables: Iterable[list[list[str | None]]]) -> list[dict]:
    """Read Gerardo Ortiz / Coral purchase orders."""
    if "GERARDO ORTIZ E HIJOS" not in text.upper():
        return []
    # The invoice uses the short document reference (BM156427/CA078886),
    # printed in the order header, rather than the numeric purchase ID.
    oc_match = re.search(r"GERARDO ORTIZ E HIJOS CIA\s+([A-Z]{2}\d+)", text, re.I)
    if not oc_match:
        oc_match = re.search(r"Ped\.\s*Compra:\s*(\d+)", text, re.I)
    oc = oc_match.group(1) if oc_match else ""
    rows = []
    for table in tables:
        if not table or "Cod.Prov" not in " ".join(str(x or "") for x in table[0]):
            continue
        for cells in table[1:]:
            if not cells or not str(cells[0] or "").strip().isdigit():
                continue
            codes = re.findall(r"\d{7,13}", str(cells[1] or ""))
            internal = next((code.zfill(8) for code in codes if len(code) <= 8), "")
            ean = next((code for code in codes if len(code) >= 12), "")
            product = re.sub(r"^[A-Z]\s*\n", "", str(cells[2] or "")).replace("\n", " ").strip()
            quantity = normalize_number(cells[4] if len(cells) > 4 else 0)
            if quantity <= 0:
                continue
            rows.append({
                "oc": oc,
                "codigo_interno": internal,
                "codigo": ean or internal,
                "producto": product,
                "cantidad": quantity,
                "precio": normalize_number(cells[5] if len(cells) > 5 else 0),
                "tipo": "pedido",
            })
    return rows


def tia_order_rows(text: str, tables: Iterable[list[list[str | None]]]) -> list[dict]:
    """Read TIA orders and use their product-homologation section for EANs."""
    if "TIENDAS INDUSTRIALES ASOCIADAS" not in text.upper():
        return []
    oc_match = re.search(r"ORDEN DE COMPRA\s*N?[º°]?\s*(\d+)", text, re.I)
    oc = oc_match.group(1) if oc_match else ""
    homologation = {}
    for line in text.splitlines():
        values = re.findall(r"(?<!\d)(\d{8,13})(?!\d)", line)
        if len(values) >= 2 and len(values[0]) == 9:
            ean = next((value for value in values[1:] if len(value) >= 12), "")
            internal = next((value.zfill(8) for value in values[1:] if len(value) == 8), "")
            homologation[values[0]] = (ean, internal)
    rows = []
    for table in tables:
        if not table:
            continue
        header_index = next((i for i, row in enumerate(table) if "CCAANNTT ((UUNNIIDD))" in str((row or [""])[0] or "")), None)
        if header_index is None:
            continue

        # TIA has changed the physical position of some columns between PDF
        # versions. The headings in these files duplicate every character
        # (for example EESSTTAADDÍÍSSTTIICCOO), so collapse those duplicates
        # and locate the fields by heading instead of relying on fixed indexes.
        def tia_heading(value: object) -> str:
            heading = re.sub(r"\s+", " ", str(value or "")).upper()
            return re.sub(r"(.)\1", r"\1", heading)

        headers = [tia_heading(cell) for cell in table[header_index]]
        quantity_index = next(
            (i for i, heading in enumerate(headers) if "CANT" in heading and "UNID" in heading), 0
        )
        product_index = next(
            (i for i, heading in enumerate(headers) if "DESCRIP" in heading), 6
        )
        statistic_index = next(
            (i for i, heading in enumerate(headers) if "ESTAD" in heading), None
        )
        price_index = next(
            (i for i, heading in enumerate(headers) if "COSTO" in heading and "UNIT" in heading), None
        )
        for cells in table[header_index + 1:]:
            if not cells:
                continue
            cells = list(cells)
            quantity_value = cells[quantity_index] if quantity_index < len(cells) else ""
            if not re.fullmatch(r"\d+(?:[.,]\d+)?", str(quantity_value or "").strip()):
                continue
            quantity = normalize_number(quantity_value)
            statistic_value = cells[statistic_index] if statistic_index is not None and statistic_index < len(cells) else ""
            client_code = re.sub(r"\D", "", str(statistic_value or ""))
            # Last-resort scan for older/newer layouts whose extracted heading
            # is blank but whose product row still contains one 9-digit code.
            if len(client_code) != 9:
                client_code = next((
                    digits for cell in cells
                    if len((digits := re.sub(r"\D", "", str(cell or "")))) == 9
                ), "")
            product = (
                str(cells[product_index] or "").replace("\n", " ").strip()
                if product_index < len(cells) else ""
            )
            # Ignore homologation/footer tables that happen to begin with a
            # numeric code but are not actual ordered product lines.
            if len(client_code) != 9 or not product:
                continue
            ean, internal = homologation.get(client_code, ("", ""))
            rows.append({
                "oc": oc,
                "codigo_interno": internal,
                "codigo": ean or internal or client_code,
                "producto": product,
                "cantidad": quantity,
                "precio": normalize_number(
                    cells[price_index] if price_index is not None and price_index < len(cells) else 0
                ),
                "tipo": "pedido",
            })
    return rows


def rosado_order_rows(text: str, tables: Iterable[list[list[str | None]]]) -> list[dict]:
    """Read El Rosado orders; CANTIDAD is cases and UXC is units per case."""
    if "CORPORACION EL ROSADO" not in text.upper():
        return []
    rows = []
    for table in tables:
        if not table:
            continue
        header_index = next((i for i, row in enumerate(table) if row and str(row[0] or "").strip() == "ITEN"), None)
        if header_index is None:
            continue
        oc = ""
        for header_row in table[:header_index]:
            if header_row and str(header_row[0] or "").strip() == "NUMERO DE ORDEN":
                oc = re.sub(r"\D", "", str(header_row[2] or ""))
                break
        for cells in table[header_index + 1:]:
            if not cells or not str(cells[0] or "").strip().isdigit():
                continue
            reference = re.sub(r"\D", "", str(cells[5] or ""))
            internal = reference.zfill(8) if 1 <= len(reference) <= 8 else ""
            ean = reference if len(reference) >= 12 else ""
            units_per_case = normalize_number(cells[7] if len(cells) > 7 else 0)
            cases = normalize_number(cells[8] if len(cells) > 8 else 0)
            if units_per_case <= 0 or cases <= 0:
                continue
            case_cost = normalize_number(cells[9] if len(cells) > 9 else 0)
            rows.append({
                "oc": oc,
                "codigo_interno": internal,
                "codigo": ean or internal,
                "producto": str(cells[2] or "").replace("\n", " ").strip(),
                "cantidad": units_per_case * cases,
                "precio": case_cost / units_per_case if units_per_case else 0,
                "tipo": "pedido",
            })
    return rows


def odoo_invoice_rows(text: str, default_oc: str) -> list[dict]:
    """Read the Odoo/SRI invoice layout used by DISLUB."""
    if "FACTURA" not in text.upper() or "CÓDIGO DESCRIPCIÓN CANTIDAD" not in text.upper():
        return []
    row_pattern = re.compile(
        r"^(?P<internal>\d{8})\s+\[(?P=internal)\]\s*(?P<body>.*?)"
        r"(?=^\d{8}\s+\[|^FORMA DE PAGO|^Page:|\Z)",
        re.M | re.S,
    )
    # Quantities may use Ecuadorian thousands separators (for example
    # 3.624,0000). Matching only the final digits would turn that into 624.
    number_with_4_decimals = r"(?:\d{1,3}(?:\.\d{3})+|\d+),\d{4}"
    qty_price = re.compile(
        rf"(?P<qty>{number_with_4_decimals})\s+(?P<price>\d+[.,]\d{{5}})"
    )
    date_match = re.search(r"Fecha de emisi[oó]n:\s*(\d{2}/\d{2}/\d{4})", text, re.I)
    document_date = (
        datetime.strptime(date_match.group(1), "%d/%m/%Y").date()
        if date_match else date.today()
    )
    rows = []
    for match in row_pattern.finditer(text):
        body = re.sub(r"\s+", " ", match.group("body")).strip()
        amounts = qty_price.search(body)
        eans = re.findall(r"(?<!\d)(\d{12,13})(?!\d)", body)
        if not amounts:
            continue
        description = body[:amounts.start()].strip(" -")
        description = re.sub(r"\s*-?\s*Ref\.?\s*$", "", description, flags=re.I)
        rows.append({
            "oc": default_oc,
            "codigo_interno": match.group("internal"),
            # A few source invoices contain a truncated 12-digit barcode.
            # The internal code remains authoritative for matching.
            "codigo": eans[-1] if eans else match.group("internal"),
            "producto": description,
            "cantidad": normalize_number(amounts.group("qty")),
            "precio": normalize_number(amounts.group("price")),
            "fecha_documento": document_date,
            "tipo": "factura",
        })
    # Keep the official document total once per invoice. This lets the UI show
    # both the taxable/base sale and the amount including VAT without counting
    # the same invoice total once for every product line.
    total_matches = re.findall(r"^Total\s+\$\s*([\d.,]+)\s*$", text, re.I | re.M)
    if rows and total_matches:
        rows[0]["total_documento"] = normalize_number(total_matches[-1])
    return rows


def odoo_invoice_bundle_rows(text: str) -> list[dict]:
    """Separate a PDF containing many consecutive Odoo invoices by page."""
    pages = re.split(r"\f", text)
    grouped: list[list[str]] = []
    current_invoice = ""
    for page in pages:
        header = re.search(r"No\.?:\s*(001-100-\d{9})", page, re.I)
        invoice_number = header.group(1) if header else current_invoice
        if header and invoice_number != current_invoice:
            grouped.append([page])
            current_invoice = invoice_number
        elif grouped:
            grouped[-1].append(page)
        elif page.strip():
            grouped.append([page])
    rows: list[dict] = []
    for pages_for_invoice in grouped:
        invoice_text = "\n".join(pages_for_invoice)
        number_match = re.search(r"No\.?:\s*(001-100-\d{9})", invoice_text, re.I)
        invoice_number = number_match.group(1) if number_match else ""
        parsed = odoo_invoice_rows(invoice_text, invoice_oc(invoice_text))
        for line_number, row in enumerate(parsed, start=1):
            row["factura_numero"] = invoice_number
            row["linea_factura"] = line_number
            row["cliente"] = known_customer(invoice_text) or "Cliente sin identificar"
        rows.extend(parsed)
    return rows


def table_rows(tables: Iterable[list[list[str | None]]], document_type: str, default_oc: str) -> list[dict]:
    rows: list[dict] = []
    aliases = {
        "codigo": ("codigo", "código", "ean", "barra", "sku", "item"),
        "producto": ("producto", "descripcion", "descripción", "articulo", "artículo"),
        "cantidad": ("cantidad", "cant", "unidades", "pedido", "facturado"),
        "precio": ("precio unitario", "precio", "p.unit", "valor unitario"),
        "oc": ("orden", "oc", "pedido"),
    }
    for table in tables:
        if not table or len(table) < 2:
            continue
        header_index = next((i for i, row in enumerate(table[:5]) if row and any("cant" in str(c or "").lower() for c in row)), None)
        if header_index is None:
            continue
        headers = [re.sub(r"\s+", " ", str(c or "")).strip().lower() for c in table[header_index]]
        positions = {}
        for field, names in aliases.items():
            positions[field] = next((i for i, h in enumerate(headers) if any(n in h for n in names)), None)
        if positions["cantidad"] is None:
            continue
        for cells in table[header_index + 1:]:
            cells = list(cells or []) + [None] * len(headers)
            quantity = normalize_number(cells[positions["cantidad"]])
            code_value = cells[positions["codigo"]] if positions["codigo"] is not None else ""
            code = clean_code(code_value)
            product = str(cells[positions["producto"]] or "").replace("\n", " ").strip() if positions["producto"] is not None else ""
            if quantity <= 0 or (not code and not product):
                continue
            rows.append({
                "oc": clean_code(cells[positions["oc"]]) if positions["oc"] is not None else default_oc,
                "codigo_interno": "",
                "codigo": code,
                "producto": product,
                "cantidad": quantity,
                "precio": normalize_number(cells[positions["precio"]]) if positions["precio"] is not None else 0,
                "tipo": document_type,
            })
    return rows


def text_rows(text: str, document_type: str, default_oc: str) -> list[dict]:
    rows = []
    # Fallback for visually separated electronic-invoice/order lines. A line must contain
    # an EAN/SKU plus at least one numeric field; table extraction remains the preferred route.
    pattern = re.compile(
        r"(?P<code>\d{8,14})\s+(?P<description>.*?\S)\s+"
        r"(?P<quantity>\d+(?:[.,]\d{1,3})?)"
        r"(?:\s+(?P<price>\d+[.,]\d{2,6}))?(?:\s|$)"
    )
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        match = pattern.search(line)
        if not match:
            continue
        quantity = normalize_number(match.group("quantity"))
        if quantity <= 0:
            continue
        rows.append({
            "oc": default_oc,
            "codigo_interno": "",
            "codigo": clean_code(match.group("code")),
            "producto": match.group("description").strip(" -"),
            "cantidad": quantity,
            "precio": normalize_number(match.group("price")),
            "tipo": document_type,
        })
    return rows


def parse_files(files, document_type: str) -> tuple[pd.DataFrame, list[str], list[str]]:
    all_rows, clients, warnings = [], [], []
    for uploaded in files:
        try:
            text, tables = extract_pdf(uploaded)
            oc, client = find_context(text)
            if not client:
                customer_markers = (
                    (r"CASA DELI", "Casa Deli"),
                    (r"GERARDO ORTIZ E HIJOS", "Coral / Gerardo Ortiz"),
                    (r"TIENDAS INDUSTRIALES ASOCIADAS", "Tía"),
                    (r"CORPORACION EL ROSADO", "Corporación El Rosado"),
                )
                client = next((name for marker, name in customer_markers if re.search(marker, text, re.I)), "")
            if client:
                clients.append(client)
            if document_type == "pedido":
                rows = (
                    favorita_order_rows(text)
                    or odoo_order_rows(text)
                    or coral_order_rows(text, tables)
                    or tia_order_rows(text, tables)
                    or rosado_order_rows(text, tables)
                )
            else:
                rows = odoo_invoice_bundle_rows(text) or odoo_invoice_rows(text, oc)
            rows = rows or table_rows(tables, document_type, oc) or text_rows(text, document_type, oc)
            rows = [row for row in rows if not excluded_product(row)]
            if document_type == "pedido":
                detected_order_client = known_customer(text) or client or "Cliente sin identificar"
                for row in rows:
                    row["cliente"] = detected_order_client
            if not rows:
                warnings.append(f"{uploaded.name}: no se reconocieron líneas de productos.")
            if document_type == "factura":
                uploaded.seek(0)
                file_hash = hashlib.sha256(uploaded.read()).hexdigest()
                uploaded.seek(0)
                for line_number, row in enumerate(rows, start=1):
                    invoice_number = str(row.get("factura_numero", "")).strip()
                    row["archivo_hash"] = (
                        hashlib.sha256(f"FACTURA:{invoice_number}".encode("utf-8")).hexdigest()
                        if invoice_number else file_hash
                    )
                    row["archivo_nombre"] = uploaded.name
                    row["linea"] = int(row.get("linea_factura", line_number))
            all_rows.extend(rows)
        except Exception as exc:
            warnings.append(f"{uploaded.name}: no se pudo leer ({exc}).")
    return pd.DataFrame(all_rows), clients, warnings


def save_invoice_ledger(invoice_rows: pd.DataFrame, business_line: str) -> None:
    """Refresh uploaded invoices in the cumulative ledger without duplicating files."""
    if invoice_rows.empty or "archivo_hash" not in invoice_rows:
        return
    with get_engine().begin() as db:
        # Remove legacy rows previously saved from an individual PDF whose
        # filename contains the same official invoice number.
        if "factura_numero" in invoice_rows:
            for invoice_number in invoice_rows["factura_numero"].dropna().unique():
                sequence = re.sub(r"\D", "", str(invoice_number))[-9:]
                if sequence:
                    db.execute(delete(facturas_acumuladas_table).where(
                        facturas_acumuladas_table.c.archivo.like(f"%{sequence}%"),
                        facturas_acumuladas_table.c.linea_negocio == business_line,
                    ))
        for file_hash in invoice_rows["archivo_hash"].dropna().unique():
            db.execute(delete(facturas_acumuladas_table).where(
                facturas_acumuladas_table.c.archivo_hash == str(file_hash),
                facturas_acumuladas_table.c.linea_negocio == business_line,
            ))
        records = []
        for _, row in invoice_rows.iterrows():
            records.append({
                "archivo_hash": str(row.get("archivo_hash", "")),
                "archivo": str(row.get("archivo_nombre", "")),
                "linea_negocio": business_line,
                "linea": int(row.get("linea", 0)),
                "fecha_documento": row.get("fecha_documento") or date.today(),
                "codigo_interno": str(row.get("codigo_interno", "")).strip().zfill(8),
                "ean": str(row.get("codigo", "")).strip(),
                "producto": str(row.get("producto", "")),
                "unidades": float(row.get("cantidad", 0)),
            })
        if records:
            db.execute(insert(facturas_acumuladas_table), records)


def forecast_dataframe(uploaded_file) -> pd.DataFrame:
    uploaded_file.seek(0)
    raw = uploaded_file.read()
    uploaded_file.seek(0)
    name = uploaded_file.name.lower()
    if name.endswith((".xlsx", ".xls")):
        source = pd.read_excel(io.BytesIO(raw))
    else:
        source = pd.read_csv(io.BytesIO(raw), sep=None, engine="python", dtype=str)
    normalized = {
        str(column).strip().lower().replace("ó", "o").replace("í", "i"): column
        for column in source.columns
    }
    code_column = next((original for key, original in normalized.items() if key in ("cod", "codigo", "codigo producto")), None)
    product_column = next((original for key, original in normalized.items() if "producto" in key or "descripcion" in key), None)
    units_column = next((original for key, original in normalized.items() if "unidad" in key or "forecast" in key), None)
    if code_column is None or units_column is None:
        raise ValueError("El archivo debe incluir las columnas COD y Unidades.")
    rows = []
    for _, item in source.iterrows():
        code_digits = re.sub(r"\D", "", str(item.get(code_column, "")))
        if not code_digits:
            continue
        product = str(item.get(product_column, "") if product_column is not None else "").strip()
        eans = re.findall(r"(?<!\d)(\d{12,13})(?!\d)", product)
        rows.append({
            "codigo_interno": code_digits.zfill(8),
            "ean": eans[-1] if eans else "",
            "producto": product,
            "unidades": normalize_number(item.get(units_column, 0)),
        })
    result = pd.DataFrame(rows)
    if result.empty:
        raise ValueError("No se encontraron productos válidos en el forecast.")
    return result.groupby("codigo_interno", as_index=False).agg(
        ean=("ean", "first"), producto=("producto", "first"), unidades=("unidades", "sum")
    )


def save_forecast(period: str, business_line: str, uploaded_file, forecast_rows: pd.DataFrame) -> int:
    storage_period = period_key(period, business_line)
    with get_engine().begin() as db:
        existing = db.execute(
            select(forecast_table.c.id).where(forecast_table.c.periodo == storage_period)
        ).first()
        if existing:
            forecast_id = int(existing.id)
            db.execute(update(forecast_table).where(forecast_table.c.id == forecast_id).values(
                archivo=uploaded_file.name, actualizado=datetime.now()
            ))
            db.execute(delete(forecast_detalle_table).where(forecast_detalle_table.c.forecast_id == forecast_id))
        else:
            created = db.execute(insert(forecast_table).values(
                periodo=storage_period, archivo=uploaded_file.name, actualizado=datetime.now()
            ))
            forecast_id = int(created.inserted_primary_key[0])
        db.execute(insert(forecast_detalle_table), [
            {
                "forecast_id": forecast_id,
                "codigo_interno": str(row.codigo_interno),
                "ean": str(row.ean),
                "producto": str(row.producto),
                "unidades": float(row.unidades),
            }
            for row in forecast_rows.itertuples(index=False)
        ])
    return forecast_id


def save_production(period: str, business_line: str, uploaded_file, production_rows: pd.DataFrame) -> int:
    """Save or replace the production assigned to a monthly forecast."""
    storage_period = period_key(period, business_line)
    with get_engine().begin() as db:
        existing = db.execute(
            select(produccion_table.c.id).where(produccion_table.c.periodo == storage_period)
        ).first()
        if existing:
            production_id = int(existing.id)
            db.execute(update(produccion_table).where(produccion_table.c.id == production_id).values(
                archivo=uploaded_file.name, actualizado=datetime.now()
            ))
            db.execute(delete(produccion_detalle_table).where(
                produccion_detalle_table.c.produccion_id == production_id
            ))
        else:
            created = db.execute(insert(produccion_table).values(
                periodo=storage_period, archivo=uploaded_file.name, actualizado=datetime.now()
            ))
            production_id = int(created.inserted_primary_key[0])
        db.execute(insert(produccion_detalle_table), [
            {
                "produccion_id": production_id,
                "codigo_interno": str(row.codigo_interno),
                "ean": str(row.ean),
                "producto": str(row.producto),
                "unidades": float(row.unidades),
            }
            for row in production_rows.itertuples(index=False)
        ])
    return production_id


def reconcile(order_rows: pd.DataFrame, invoice_rows: pd.DataFrame) -> pd.DataFrame:
    if order_rows.empty:
        raise ValueError("No se encontraron líneas válidas en los pedidos.")
    orders = order_rows.copy()
    invoices = invoice_rows.copy()
    for frame in (orders, invoices):
        for column in ("oc", "codigo", "codigo_interno", "producto"):
            if column not in frame:
                frame[column] = ""
            frame[column] = frame[column].fillna("").astype(str).str.strip()
        if "cantidad" not in frame:
            frame["cantidad"] = 0.0
        if "precio" not in frame:
            frame["precio"] = 0.0
    if "cliente" not in orders:
        orders["cliente"] = "Cliente sin identificar"
    orders["cliente"] = orders["cliente"].fillna("Cliente sin identificar").astype(str).str.strip()

    def normalize_internal(value: object) -> str:
        digits = re.sub(r"\D", "", str(value or ""))
        return digits.zfill(8) if digits and len(digits) <= 8 else digits

    for frame in (orders, invoices):
        frame["codigo"] = frame["codigo"].map(clean_code)
        frame["codigo_interno"] = frame["codigo_interno"].map(normalize_internal)

    # The invoice normally contains both identifiers. Use it as a homologation
    # table so an order carrying only EAN can still match by internal code, but
    # only when that EAN maps to one internal code. Some customers reuse the
    # same EAN with a different internal reference, so a global first-match
    # mapping would assign those orders to the wrong product.
    invoice_codes = invoices[
        (invoices["codigo"] != "") & (invoices["codigo_interno"] != "")
    ].groupby("codigo")["codigo_interno"].agg(lambda values: list(dict.fromkeys(values)))
    ean_to_internal = {
        ean: internal_codes[0]
        for ean, internal_codes in invoice_codes.items()
        if len(internal_codes) == 1
    }
    orders["codigo_interno"] = orders.apply(
        lambda row: row.codigo_interno or ean_to_internal.get(row.codigo, ""), axis=1
    )

    # If an invoice omits its OC, infer it only when that product appears in
    # exactly one of the uploaded orders. Ambiguous products remain unmatched
    # instead of being assigned to the wrong customer order.
    orders["identity"] = orders.apply(lambda row: row.codigo_interno or row.codigo, axis=1)
    invoices["identity"] = invoices.apply(lambda row: row.codigo_interno or row.codigo, axis=1)
    unique_order_oc = (
        orders[orders["oc"] != ""]
        .groupby("identity")["oc"]
        .agg(lambda values: list(dict.fromkeys(values)))
        .to_dict()
    )
    invoices["oc"] = invoices.apply(
        lambda row: (
            unique_order_oc[row.identity][0]
            if not row.oc and len(unique_order_oc.get(row.identity, [])) == 1
            else row.oc
        ),
        axis=1,
    )

    def normalized_product(value: object) -> str:
        value = unicodedata.normalize("NFKD", str(value or ""))
        value = "".join(character for character in value if not unicodedata.combining(character))
        value = re.sub(r"(?<=\d)\s+(?=[A-Z])", "", value.upper())
        words = re.findall(r"[A-Z0-9]+", value)
        ignored = {"DE", "DEL", "LA", "EL", "EN", "DOYPACK", "FRASCO"}
        normalized = []
        for word in words:
            if word in ignored:
                continue
            # A light singularization makes SEMILLA/SEMILLAS and similar
            # descriptions comparable without changing their meaning.
            if len(word) > 4 and word.endswith("S"):
                word = word[:-1]
            normalized.append(word)
        return " ".join(normalized)

    orders["product_key"] = orders["producto"].map(normalized_product)
    invoices["product_key"] = invoices["producto"].map(normalized_product)
    orders["match_key"] = orders.apply(
        lambda row: f"{row.oc}|{row.codigo_interno or row.codigo or row.product_key}", axis=1
    )

    def invoice_match_key(row: pd.Series) -> str:
        candidates = orders[orders["oc"] == row.oc] if row.oc else orders
        if candidates.empty:
            return f"{row.oc}|{row.codigo_interno or row.codigo or row.product_key}"

        if row.codigo_interno:
            exact_internal = candidates[candidates["codigo_interno"] == row.codigo_interno]
            if len(exact_internal) == 1:
                return str(exact_internal.iloc[0].match_key)
        if row.codigo:
            exact_ean = candidates[candidates["codigo"] == row.codigo]
            if len(exact_ean) == 1:
                return str(exact_ean.iloc[0].match_key)

        # TIA occasionally prints "No Tiene Productos Homologados". In those
        # orders there is no supplier EAN/internal code, so use the product
        # description only inside the same OC and only for a clear match.
        if row.product_key:
            scored = [
                (SequenceMatcher(None, row.product_key, candidate.product_key).ratio(), candidate)
                for candidate in candidates.itertuples()
                if candidate.product_key
            ]
            scored.sort(key=lambda item: item[0], reverse=True)
            if scored and scored[0][0] >= 0.72 and (
                len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.08
            ):
                return str(scored[0][1].match_key)
        return f"{row.oc}|{row.codigo_interno or row.codigo or row.product_key}"

    invoices["match_key"] = invoices.apply(invoice_match_key, axis=1)
    order_group = orders.groupby("match_key", as_index=False).agg(
        cliente=("cliente", "first"), oc=("oc", "first"),
        codigo=("codigo", "first"), producto=("producto", "first"),
        pedidas=("cantidad", "sum"), precio_unitario=("precio", "max"),
    )
    billed = invoices.groupby("match_key", as_index=False)["cantidad"].sum().rename(columns={"cantidad": "facturadas_raw"}) if not invoices.empty else pd.DataFrame(columns=["match_key", "facturadas_raw"])
    result = order_group.merge(billed, on="match_key", how="left")
    result["facturadas_raw"] = result["facturadas_raw"].fillna(0.0)
    result["facturadas"] = result[["pedidas", "facturadas_raw"]].min(axis=1)
    result["pendientes"] = (result["pedidas"] - result["facturadas"]).clip(lower=0)
    result["venta_potencial"] = result["pedidas"] * result["precio_unitario"]
    result["venta_facturada"] = result["facturadas"] * result["precio_unitario"]
    result["venta_perdida"] = result["pendientes"] * result["precio_unitario"]
    result["fill_rate"] = result.apply(lambda r: 100 * r.facturadas / r.pedidas if r.pedidas else 0.0, axis=1)
    detail = result[["cliente", *DETAIL_COLUMNS]].sort_values(["cliente", "oc", "producto"]).reset_index(drop=True)
    detail.attrs["total_con_iva"] = (
        float(pd.to_numeric(invoices.get("total_documento", 0), errors="coerce").fillna(0).sum())
        if "total_documento" in invoices else 0.0
    )
    return detail


def totals(detail: pd.DataFrame) -> dict[str, float]:
    ordered = float(detail["pedidas"].sum())
    billed = float(detail["facturadas"].sum())
    summary = {
        "unidades_pedidas": ordered,
        "unidades_facturadas": billed,
        "fill_rate": (100 * billed / ordered) if ordered else 0.0,
        "venta_potencial": float(detail["venta_potencial"].sum()),
        "venta_facturada": float(detail["venta_facturada"].sum()),
        "venta_perdida": float(detail["venta_perdida"].sum()),
    }
    summary["total_con_iva"] = float(detail.attrs.get("total_con_iva", summary["venta_facturada"]))
    return summary


def customer_unit_summary(detail: pd.DataFrame) -> pd.DataFrame:
    """Summarize requested, billed and pending units for each customer."""
    if "cliente" not in detail or detail.empty:
        return pd.DataFrame()
    summary = detail.groupby("cliente", as_index=False).agg(
        unidades_solicitadas=("pedidas", "sum"),
        unidades_facturadas=("facturadas", "sum"),
        unidades_pendientes=("pendientes", "sum"),
    )
    summary["cumplimiento"] = summary.apply(
        lambda row: 100 * row.unidades_facturadas / row.unidades_solicitadas
        if row.unidades_solicitadas else 0.0,
        axis=1,
    )
    return summary.sort_values("cliente").reset_index(drop=True)


def clear_operational_data() -> None:
    """Delete test runs and billed units while preserving every forecast row."""
    with get_engine().begin() as db:
        db.execute(delete(detalle_table))
        db.execute(delete(cargas_table))
        db.execute(delete(facturas_acumuladas_table))


def delete_saved_run(run_id: int) -> tuple[list[str], list[str]]:
    """Delete one saved run and its unreferenced invoices from the monthly ledger."""
    with get_engine().begin() as db:
        saved = db.execute(select(
            cargas_table.c.nombres_pedidos,
            cargas_table.c.nombres_facturas,
            cargas_table.c.linea_negocio,
        ).where(cargas_table.c.id == int(run_id))).first()
        if not saved:
            return [], []
        try:
            order_names = list(json.loads(saved.nombres_pedidos or "[]"))
        except (TypeError, json.JSONDecodeError):
            order_names = []
        try:
            invoice_names = list(json.loads(saved.nombres_facturas or "[]"))
        except (TypeError, json.JSONDecodeError):
            invoice_names = []

        # Keep ledger rows if another saved run still references that invoice.
        remaining = db.execute(select(cargas_table.c.nombres_facturas).where(
            cargas_table.c.id != int(run_id)
        )).all()
        referenced_elsewhere: set[str] = set()
        for row in remaining:
            try:
                referenced_elsewhere.update(json.loads(row.nombres_facturas or "[]"))
            except (TypeError, json.JSONDecodeError):
                continue
        removable_invoices = [name for name in invoice_names if name not in referenced_elsewhere]
        if removable_invoices:
            db.execute(delete(facturas_acumuladas_table).where(
                facturas_acumuladas_table.c.archivo.in_(removable_invoices),
                facturas_acumuladas_table.c.linea_negocio == saved.linea_negocio,
            ))
        db.execute(delete(detalle_table).where(detalle_table.c.carga_id == int(run_id)))
        db.execute(delete(cargas_table).where(cargas_table.c.id == int(run_id)))
    return order_names, invoice_names


def file_fingerprint(order_files, invoice_files, business_line: str = "Karay") -> str:
    digest = hashlib.sha256()
    digest.update(business_line.encode("utf-8"))
    for role, files in (("PEDIDO", order_files), ("FACTURA", invoice_files)):
        digest.update(role.encode("ascii"))
        for uploaded in sorted(files, key=lambda f: f.name):
            uploaded.seek(0)
            digest.update(uploaded.name.encode("utf-8"))
            digest.update(uploaded.read())
            uploaded.seek(0)
    return digest.hexdigest()


def save_run(
    detail: pd.DataFrame, summary: dict, cliente: str, usuario: str,
    business_line: str, order_files, invoice_files,
) -> tuple[int, bool]:
    fingerprint = file_fingerprint(order_files, invoice_files, business_line)
    with get_engine().begin() as db:
        existing = db.execute(select(cargas_table.c.id).where(cargas_table.c.fingerprint == fingerprint)).first()
        values = dict(
            fecha=datetime.now(), cliente=cliente, usuario=usuario,
            linea_negocio=business_line,
            fill_rate=float(summary["fill_rate"]), venta_potencial=float(summary["venta_potencial"]),
            venta_facturada=float(summary["venta_facturada"]), venta_perdida=float(summary["venta_perdida"]),
            unidades_pedidas=float(summary["unidades_pedidas"]),
            unidades_facturadas=float(summary["unidades_facturadas"]),
            pedidos_archivos=len(order_files), facturas_archivos=len(invoice_files),
            nombres_pedidos=json.dumps([f.name for f in order_files], ensure_ascii=False),
            nombres_facturas=json.dumps([f.name for f in invoice_files], ensure_ascii=False),
            fingerprint=fingerprint,
        )
        if existing:
            run_id = int(existing.id)
            db.execute(update(cargas_table).where(cargas_table.c.id == run_id).values(**values))
            db.execute(delete(detalle_table).where(detalle_table.c.carga_id == run_id))
            created = False
        else:
            result = db.execute(insert(cargas_table).values(**values))
            run_id = int(result.inserted_primary_key[0])
            created = True
        records = []
        for _, row in detail.iterrows():
            record = {"carga_id": run_id}
            for column in DETAIL_COLUMNS:
                record[column] = str(row[column]) if column in ("oc", "codigo", "producto") else float(row[column])
            records.append(record)
        db.execute(insert(detalle_table), records)
    return run_id, created


def money(value: float) -> str:
    return f"${value:,.2f}"


def show_metrics(summary: dict) -> None:
    columns = st.columns(5)
    columns[0].metric("Fill Rate", f"{summary['fill_rate']:.2f}%")
    columns[1].metric("Venta potencial", money(summary["venta_potencial"]))
    columns[2].metric("Venta facturada", money(summary["venta_facturada"]))
    columns[3].metric("Total facturas con IVA", money(summary["total_con_iva"]))
    columns[4].metric("Venta dejada de facturar", money(summary["venta_perdida"]))
    st.caption(f"{summary['unidades_facturadas']:,.0f} de {summary['unidades_pedidas']:,.0f} unidades atendidas")


def processing_tab() -> None:
    st.subheader("Procesar Fill Rate")
    st.write("Carga uno o varios pedidos y facturas en PDF. Los resultados se consolidan y guardan en el histórico.")
    business_line = st.selectbox(
        "Línea de negocio",
        BUSINESS_LINES,
        key="processing_business_line",
        help="Separa el Histórico y los Indicadores de Karay y Lácteos El Pino.",
    )
    left, right = st.columns(2)
    # Some customers deliver valid PDF documents without the .pdf extension
    # (for example El Rosado). Leaving the extension filter open lets those
    # files through; pdfplumber still validates their real content on reading.
    order_files = left.file_uploader(
        "Pedidos PDF (también sin extensión)", accept_multiple_files=True, key="orders"
    )
    invoice_files = right.file_uploader(
        "Facturas PDF (también sin extensión)", accept_multiple_files=True, key="invoices"
    )
    meta_left, meta_right = st.columns(2)
    cliente = meta_left.text_input("Cliente (opcional)", placeholder="Ej. Corporación Favorita")
    usuario = meta_right.text_input("Responsable", value=st.session_state.get("username", "Operador"), disabled=True)

    action_left, action_right = st.columns(2)
    process_clicked = action_left.button("1. Procesar y revisar", width="stretch")

    if process_clicked:
        if not order_files or not invoice_files:
            st.error("Selecciona al menos un pedido y una factura PDF.")
            return
        with st.spinner("Leyendo y conciliando los documentos..."):
            order_rows, order_clients, order_warnings = parse_files(order_files, "pedido")
            invoice_rows, invoice_clients, invoice_warnings = parse_files(invoice_files, "factura")
            try:
                detail = reconcile(order_rows, invoice_rows)
            except ValueError as exc:
                st.error(str(exc))
                for warning in order_warnings + invoice_warnings:
                    st.warning(warning)
                return
            summary = totals(detail)
            detected_client = cliente.strip() or next(iter(order_clients + invoice_clients), "Sin especificar")
            st.session_state["pending_result"] = {
                "detail": detail,
                "summary": summary,
                "invoice_rows": invoice_rows,
                "cliente": detected_client,
                "usuario": usuario.strip() or "Operador",
                "business_line": business_line,
                "fingerprint": file_fingerprint(order_files, invoice_files, business_line),
                "warnings": order_warnings + invoice_warnings,
            }
            st.session_state["last_result"] = (detail, summary)
        st.info(
            "Resultados procesados para revisión. Todavía no se guardaron en el Histórico "
            "ni se actualizó el Forecast. Revisa la información y luego pulsa Guardar."
        )
        for warning in order_warnings + invoice_warnings:
            st.warning(warning)

    save_clicked = action_right.button(
        "2. Guardar y actualizar Forecast",
        type="primary",
        width="stretch",
        disabled="pending_result" not in st.session_state,
    )
    if save_clicked:
        pending = st.session_state.get("pending_result")
        if not pending or not order_files or not invoice_files:
            st.error("Primero carga los archivos y pulsa Procesar y revisar.")
        elif business_line != pending["business_line"]:
            st.error("La línea de negocio cambió después de la revisión. Procesa nuevamente.")
        elif file_fingerprint(order_files, invoice_files, business_line) != pending["fingerprint"]:
            st.error("Los archivos cambiaron después de la revisión. Pulsa nuevamente Procesar y revisar.")
        else:
            with st.spinner("Guardando el Histórico y actualizando el Forecast..."):
                # The Forecast reads this cumulative, invoice-level ledger.
                # Nothing reaches it until the user explicitly confirms Save.
                save_invoice_ledger(pending["invoice_rows"], pending["business_line"])
                run_id, created = save_run(
                    pending["detail"], pending["summary"], pending["cliente"],
                    pending["usuario"], pending["business_line"], order_files, invoice_files,
                )
            del st.session_state["pending_result"]
            if created:
                st.success(
                    f"Procesamiento #{run_id} guardado. El Histórico y el Forecast fueron actualizados."
                )
            else:
                st.info(
                    f"Procesamiento #{run_id} actualizado sin duplicados. El Forecast fue recalculado."
                )

    if "last_result" in st.session_state:
        detail, summary = st.session_state["last_result"]
        show_metrics(summary)
        customer_summary = customer_unit_summary(detail)
        if not customer_summary.empty:
            st.markdown("### Cumplimiento por cliente (unidades)")
            st.dataframe(customer_summary, width="stretch", hide_index=True, column_config={
                "cliente": st.column_config.TextColumn("Cliente"),
                "unidades_solicitadas": st.column_config.NumberColumn("Unidades solicitadas", format="%.0f"),
                "unidades_facturadas": st.column_config.NumberColumn("Unidades facturadas", format="%.0f"),
                "unidades_pendientes": st.column_config.NumberColumn("Unidades pendientes", format="%.0f"),
                "cumplimiento": st.column_config.ProgressColumn(
                    "Cumplimiento", min_value=0, max_value=100, format="%.2f%%"
                ),
            })
            st.download_button(
                "Descargar resumen por cliente CSV",
                customer_summary.to_csv(index=False).encode("utf-8-sig"),
                "cumplimiento_por_cliente.csv",
                "text/csv",
            )
        st.dataframe(detail, width="stretch", hide_index=True, column_config={
            "fill_rate": st.column_config.NumberColumn("Fill Rate", format="%.2f%%"),
            "precio_unitario": st.column_config.NumberColumn("Precio unitario", format="$%.2f"),
            "venta_potencial": st.column_config.NumberColumn("Venta potencial", format="$%.2f"),
            "venta_facturada": st.column_config.NumberColumn("Venta facturada", format="$%.2f"),
            "venta_perdida": st.column_config.NumberColumn("Venta perdida", format="$%.2f"),
        })
        st.download_button("Descargar detalle CSV", detail.to_csv(index=False).encode("utf-8-sig"), "fill_rate.csv", "text/csv")


def history_tab() -> None:
    st.subheader("Histórico")
    with get_engine().connect() as db:
        history = pd.read_sql(select(cargas_table).order_by(cargas_table.c.fecha.desc()), db)

    if history.empty:
        st.info("Todavía no hay procesamientos guardados.")
        return
    history["fecha_dt"] = pd.to_datetime(history["fecha"])
    c1, c2, c3, c4 = st.columns(4)
    start = c1.date_input("Desde", value=history["fecha_dt"].dt.date.min())
    end = c2.date_input("Hasta", value=date.today())
    clients = sorted(x for x in history["cliente"].dropna().unique() if x)
    client = c3.selectbox("Cliente", ["Todos", *clients])
    business_line = c4.selectbox("Línea de negocio", ["Todas", *BUSINESS_LINES], key="history_line")
    filtered = history[(history["fecha_dt"].dt.date >= start) & (history["fecha_dt"].dt.date <= end)]
    if client != "Todos":
        filtered = filtered[filtered["cliente"] == client]
    if business_line != "Todas":
        filtered = filtered[filtered["linea_negocio"] == business_line]
    display = filtered[["id", "fecha_dt", "linea_negocio", "cliente", "usuario", "fill_rate", "venta_perdida", "pedidos_archivos", "facturas_archivos"]].copy()
    display.columns = ["ID", "Fecha", "Línea de negocio", "Cliente", "Usuario", "Fill Rate", "Venta perdida", "Pedidos", "Facturas"]
    st.dataframe(display, width="stretch", hide_index=True, column_config={
        "Fecha": st.column_config.DatetimeColumn(format="DD/MM/YYYY HH:mm"),
        "Fill Rate": st.column_config.NumberColumn(format="%.2f%%"),
        "Venta perdida": st.column_config.NumberColumn(format="$%.2f"),
    })
    if filtered.empty:
        return
    selected = st.selectbox(
        "Ver detalle del procesamiento",
        [None, *filtered["id"].tolist()],
        format_func=lambda x: "Selecciona una carga" if x is None else f"Procesamiento #{x}",
        index=0,
        key="selected_history_run",
    )
    if selected is None:
        st.info("Selecciona una carga para ver su detalle o eliminarla individualmente.")
        return
    selected_row = history.loc[history["id"] == int(selected)].iloc[0]
    try:
        selected_orders = json.loads(selected_row.get("nombres_pedidos") or "[]")
    except (TypeError, json.JSONDecodeError):
        selected_orders = []
    try:
        selected_invoices = json.loads(selected_row.get("nombres_facturas") or "[]")
    except (TypeError, json.JSONDecodeError):
        selected_invoices = []

    with st.expander("✏️ Modificar o eliminar esta carga"):
        st.error(
            f"Se eliminará únicamente el procesamiento #{selected}, del "
            f"{pd.to_datetime(selected_row['fecha']).strftime('%d/%m/%Y %H:%M')}, "
            f"cliente {selected_row.get('cliente') or 'sin cliente'}."
        )
        st.write(
            "Elimina este procesamiento para corregir sus archivos y volverlos a cargar desde "
            "**📦 Procesar pedidos**. También se retirarán sus facturas del acumulado de indicadores."
        )
        files_left, files_right = st.columns(2)
        files_left.caption("Pedidos de esta carga")
        files_left.write("\n".join(f"• {name}" for name in selected_orders) or "Sin archivos registrados")
        files_right.caption("Facturas de esta carga")
        files_right.write("\n".join(f"• {name}" for name in selected_invoices) or "Sin archivos registrados")
        confirmation_id = st.text_input(
            f"Para confirmar, escribe el número {selected}",
            key=f"confirm_delete_run_{selected}",
            placeholder=str(selected),
        )
        if st.button(
            f"Eliminar solamente el procesamiento #{selected}",
            type="primary",
            disabled=confirmation_id.strip() != str(selected),
            key=f"delete_run_{selected}",
        ):
            delete_saved_run(int(selected))
            st.session_state.pop("last_result", None)
            st.session_state.pop("pending_result", None)
            st.success(
                f"Procesamiento #{selected} eliminado. Ya puedes corregir y volver a cargar sus archivos."
            )
            st.rerun()

    detail_columns = [detalle_table.c[c] for c in DETAIL_COLUMNS]
    with get_engine().connect() as db:
        detail = pd.read_sql(
            select(*detail_columns).where(detalle_table.c.carga_id == int(selected)).order_by(detalle_table.c.oc, detalle_table.c.producto),
            db,
        )
    st.dataframe(detail, width="stretch", hide_index=True)
    st.download_button("Descargar este detalle", detail.to_csv(index=False).encode("utf-8-sig"), f"fill_rate_{selected}.csv", "text/csv")


def forecast_tab() -> None:
    st.subheader("📊 Indicadores")
    st.write("Control mensual por unidades: pedidos, entregas, producción y forecast.")
    indicator_line = st.selectbox(
        "Línea de negocio",
        BUSINESS_LINES,
        key="indicator_business_line",
    )

    with st.expander("⚙️ Cargar o reemplazar forecast", expanded=False):
        upload_left, upload_right = st.columns([2, 1])
        forecast_file = upload_left.file_uploader(
            "Forecast Excel, CSV o TXT", type=["xlsx", "xls", "csv", "txt"], key="forecast_file"
        )
        month_value = upload_right.date_input(
            "Mes del forecast", value=date.today().replace(day=1), key="forecast_month"
        )
        if st.button("Guardar o reemplazar forecast", type="primary", width="stretch"):
            if forecast_file is None:
                st.error("Selecciona el archivo del forecast.")
            else:
                try:
                    rows = forecast_dataframe(forecast_file)
                    period = month_value.strftime("%Y-%m")
                    save_forecast(period, indicator_line, forecast_file, rows)
                    st.success(
                        f"Forecast {period} de {indicator_line} guardado: {len(rows)} productos y "
                        f"{rows['unidades'].sum():,.0f} unidades."
                    )
                except Exception as exc:
                    st.error(f"No se pudo cargar el forecast: {exc}")

    with get_engine().connect() as db:
        available = pd.read_sql(select(forecast_table).order_by(forecast_table.c.periodo.desc()), db)
    if not available.empty:
        available["linea_negocio"] = available["periodo"].map(line_from_period_key)
        available["periodo_visible"] = available["periodo"].map(period_from_key)
        available = available[available["linea_negocio"] == indicator_line].copy()
    if available.empty:
        st.info(f"Carga el primer forecast de {indicator_line} para habilitar los indicadores.")
        return

    selected_period = st.selectbox(
        "Periodo de análisis", available["periodo_visible"].tolist(),
        key=f"indicator_period_{indicator_line}",
    )
    forecast_id = int(available.loc[available["periodo_visible"] == selected_period, "id"].iloc[0])
    year, month = map(int, selected_period.split("-"))
    period_start = date(year, month, 1)
    period_end = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    start_dt = datetime.combine(period_start, datetime.min.time())
    end_dt = datetime.combine(period_end, datetime.min.time())

    with get_engine().connect() as db:
        forecast_rows = pd.read_sql(
            select(
                forecast_detalle_table.c.codigo_interno,
                forecast_detalle_table.c.ean,
                forecast_detalle_table.c.producto,
                forecast_detalle_table.c.unidades.label("forecast"),
            ).where(forecast_detalle_table.c.forecast_id == forecast_id), db,
        )
        order_detail = pd.read_sql(
            select(
                cargas_table.c.cliente,
                detalle_table.c.codigo,
                detalle_table.c.producto,
                detalle_table.c.pedidas,
                detalle_table.c.facturadas,
                detalle_table.c.venta_potencial,
                detalle_table.c.venta_facturada,
                detalle_table.c.venta_perdida,
            ).select_from(detalle_table.join(cargas_table, detalle_table.c.carga_id == cargas_table.c.id)).where(
                cargas_table.c.fecha >= start_dt,
                cargas_table.c.fecha < end_dt,
                cargas_table.c.linea_negocio == indicator_line,
            ), db,
        )
    if not order_detail.empty:
        order_detail = order_detail[
            ~order_detail.apply(excluded_product, axis=1)
        ].reset_index(drop=True)

    # Relate order codes to the internal forecast code using either COD or EAN.
    internal_codes = set(forecast_rows["codigo_interno"].astype(str))
    ean_to_internal = {
        str(row.ean): str(row.codigo_interno)
        for row in forecast_rows.itertuples(index=False) if str(row.ean).strip()
    }
    if not order_detail.empty:
        def indicator_code(value: object) -> str:
            digits = re.sub(r"\D", "", str(value or ""))
            internal = digits.zfill(8) if digits and len(digits) <= 8 else digits
            if internal in internal_codes:
                return internal
            return ean_to_internal.get(digits, internal)
        order_detail["codigo_interno"] = order_detail["codigo"].map(indicator_code)

    fill_tab, production_tab, accuracy_tab = st.tabs([
        "🚚 Fill Rate", "🏭 Nivel de Servicio de Producción", "🎯 Exactitud del Forecast"
    ])

    with fill_tab:
        st.markdown("#### Pedidas vs. entregadas")
        if order_detail.empty:
            st.warning("No hay pedidos guardados en este periodo.")
        else:
            requested = float(order_detail["pedidas"].sum())
            delivered = float(order_detail["facturadas"].sum())
            pending = max(requested - delivered, 0.0)
            fill_rate = 100 * delivered / requested if requested else 0.0
            cards = st.columns(4)
            cards[0].metric("Unidades pedidas", f"{requested:,.0f}")
            cards[1].metric("Unidades entregadas", f"{delivered:,.0f}")
            cards[2].metric("Unidades pendientes", f"{pending:,.0f}")
            cards[3].metric("Fill Rate general", f"{fill_rate:.1f}%")
            st.progress(min(max(fill_rate / 100, 0.0), 1.0))

            st.markdown("#### Cumplimiento en dólares")
            potential_sales = float(order_detail["venta_potencial"].sum())
            billed_sales = float(order_detail["venta_facturada"].sum())
            missed_sales = max(potential_sales - billed_sales, 0.0)
            value_fill_rate = 100 * billed_sales / potential_sales if potential_sales else 0.0
            value_cards = st.columns(4)
            value_cards[0].metric("Venta potencial", f"${potential_sales:,.2f}")
            value_cards[1].metric("Venta facturada", f"${billed_sales:,.2f}")
            value_cards[2].metric("Venta dejada de facturar", f"${missed_sales:,.2f}")
            value_cards[3].metric("Fill Rate en dólares", f"{value_fill_rate:.1f}%")
            st.progress(min(max(value_fill_rate / 100, 0.0), 1.0))

            by_customer = order_detail.groupby("cliente", as_index=False).agg(
                pedidas=("pedidas", "sum"),
                entregadas=("facturadas", "sum"),
                venta_potencial=("venta_potencial", "sum"),
                venta_facturada=("venta_facturada", "sum"),
            )
            by_customer["pendientes"] = (by_customer["pedidas"] - by_customer["entregadas"]).clip(lower=0)
            by_customer["fill_rate"] = by_customer.apply(
                lambda row: 100 * row.entregadas / row.pedidas if row.pedidas else 0.0, axis=1
            )
            by_customer["venta_pendiente"] = (
                by_customer["venta_potencial"] - by_customer["venta_facturada"]
            ).clip(lower=0)
            by_customer["fill_rate_dolares"] = by_customer.apply(
                lambda row: 100 * row.venta_facturada / row.venta_potencial
                if row.venta_potencial else 0.0,
                axis=1,
            )
            by_product = order_detail.groupby(
                ["codigo_interno", "producto"], as_index=False
            ).agg(
                pedidas=("pedidas", "sum"),
                entregadas=("facturadas", "sum"),
                venta_potencial=("venta_potencial", "sum"),
                venta_facturada=("venta_facturada", "sum"),
            )
            by_product["pendientes"] = (by_product["pedidas"] - by_product["entregadas"]).clip(lower=0)
            by_product["fill_rate"] = by_product.apply(
                lambda row: 100 * row.entregadas / row.pedidas if row.pedidas else 0.0, axis=1
            )
            by_product["venta_pendiente"] = (
                by_product["venta_potencial"] - by_product["venta_facturada"]
            ).clip(lower=0)
            by_product["fill_rate_dolares"] = by_product.apply(
                lambda row: 100 * row.venta_facturada / row.venta_potencial
                if row.venta_potencial else 0.0,
                axis=1,
            )
            money_columns = {
                "venta_potencial": st.column_config.NumberColumn("Venta potencial", format="$%.2f"),
                "venta_facturada": st.column_config.NumberColumn("Venta facturada", format="$%.2f"),
                "venta_pendiente": st.column_config.NumberColumn("Venta pendiente", format="$%.2f"),
                "fill_rate": st.column_config.NumberColumn("Fill Rate unidades", format="%.1f%%"),
                "fill_rate_dolares": st.column_config.NumberColumn("Fill Rate dólares", format="%.1f%%"),
            }
            left, right = st.columns(2)
            left.markdown("##### Resultado por cliente")
            left.dataframe(
                by_customer.sort_values("fill_rate"), width="stretch", hide_index=True,
                column_config=money_columns,
            )
            right.markdown("##### Resultado por producto")
            right.dataframe(
                by_product.sort_values("fill_rate"), width="stretch", hide_index=True,
                column_config=money_columns,
            )

    with production_tab:
        st.markdown("#### Producción destinada al forecast vs. forecast")
        production_file = st.file_uploader(
            "Producción del mes (columnas COD y Unidades)",
            type=["xlsx", "xls", "csv", "txt"],
            key=f"production_{selected_period}_{indicator_line}"
        )
        if st.button(
            "Guardar o reemplazar producción", type="primary",
            key=f"save_production_{selected_period}_{indicator_line}",
        ):
            if production_file is None:
                st.error("Selecciona el archivo de producción.")
            else:
                try:
                    production_rows = forecast_dataframe(production_file)
                    save_production(selected_period, indicator_line, production_file, production_rows)
                    st.success(
                        f"Producción {selected_period} guardada: "
                        f"{production_rows['unidades'].sum():,.0f} unidades."
                    )
                except Exception as exc:
                    st.error(f"No se pudo cargar la producción: {exc}")

        with get_engine().connect() as db:
            production_header = db.execute(
                select(produccion_table.c.id).where(
                    produccion_table.c.periodo == period_key(selected_period, indicator_line)
                )
            ).first()
            if production_header:
                produced = pd.read_sql(
                    select(
                        produccion_detalle_table.c.codigo_interno,
                        produccion_detalle_table.c.unidades.label("producido"),
                    ).where(produccion_detalle_table.c.produccion_id == int(production_header.id)), db,
                )
            else:
                produced = pd.DataFrame(columns=["codigo_interno", "producido"])

        production_report = forecast_rows.merge(produced, on="codigo_interno", how="left")
        production_report["producido"] = production_report["producido"].fillna(0.0)
        production_report["pendiente_producir"] = (
            production_report["forecast"] - production_report["producido"]
        ).clip(lower=0)
        production_report["excedente"] = (
            production_report["producido"] - production_report["forecast"]
        ).clip(lower=0)
        production_report["nivel_servicio"] = production_report.apply(
            lambda row: 100 * row.producido / row.forecast if row.forecast else 0.0, axis=1
        )
        forecast_total = float(production_report["forecast"].sum())
        produced_total = float(production_report["producido"].sum())
        service = 100 * produced_total / forecast_total if forecast_total else 0.0
        p_cards = st.columns(5)
        p_cards[0].metric("Forecast", f"{forecast_total:,.0f}")
        p_cards[1].metric("Producido", f"{produced_total:,.0f}")
        p_cards[2].metric("Pendiente de producir", f"{production_report['pendiente_producir'].sum():,.0f}")
        p_cards[3].metric("Excedente", f"{production_report['excedente'].sum():,.0f}")
        p_cards[4].metric("Nivel de servicio", f"{service:.1f}%")
        if produced.empty:
            st.info("Carga la producción del mes para calcular este indicador.")
        st.dataframe(
            production_report[["codigo_interno", "producto", "forecast", "producido", "pendiente_producir", "excedente", "nivel_servicio"]]
            .sort_values("pendiente_producir", ascending=False), width="stretch", hide_index=True
        )

    with accuracy_tab:
        st.markdown("#### Forecast vs. pedidos reales")
        if order_detail.empty:
            st.warning("No hay pedidos reales guardados en este periodo.")
        else:
            real_orders = order_detail.groupby("codigo_interno", as_index=False)["pedidas"].sum().rename(
                columns={"pedidas": "pedido_real"}
            )
            accuracy = forecast_rows.merge(real_orders, on="codigo_interno", how="left")
            accuracy["pedido_real"] = accuracy["pedido_real"].fillna(0.0)
            accuracy["diferencia"] = accuracy["forecast"] - accuracy["pedido_real"]
            accuracy["sesgo"] = accuracy["diferencia"].apply(
                lambda value: "Sobreestimación" if value > 0 else ("Subestimación" if value < 0 else "Exacto")
            )
            accuracy["exactitud"] = accuracy.apply(
                lambda row: max(0.0, 100 * (1 - abs(row.pedido_real - row.forecast) / row.pedido_real))
                if row.pedido_real > 0 else (100.0 if row.forecast == 0 else 0.0), axis=1
            )
            forecast_total = float(accuracy["forecast"].sum())
            real_total = float(accuracy["pedido_real"].sum())
            general_accuracy = max(0.0, 100 * (1 - abs(real_total - forecast_total) / real_total)) if real_total else 0.0
            over = float(accuracy.loc[accuracy["diferencia"] > 0, "diferencia"].sum())
            under = float((-accuracy.loc[accuracy["diferencia"] < 0, "diferencia"]).sum())
            a_cards = st.columns(5)
            a_cards[0].metric("Forecast", f"{forecast_total:,.0f}")
            a_cards[1].metric("Pedido real", f"{real_total:,.0f}")
            a_cards[2].metric("Exactitud general", f"{general_accuracy:.1f}%")
            a_cards[3].metric("Sobreestimación", f"{over:,.0f}")
            a_cards[4].metric("Subestimación", f"{under:,.0f}")
            st.dataframe(
                accuracy[["codigo_interno", "producto", "forecast", "pedido_real", "diferencia", "sesgo", "exactitud"]]
                .sort_values("exactitud"), width="stretch", hide_index=True
            )
            st.download_button(
                "Descargar exactitud CSV",
                accuracy.to_csv(index=False).encode("utf-8-sig"),
                f"exactitud_forecast_{selected_period}.csv", "text/csv",
            )


def configured_users() -> dict[str, str]:
    try:
        users = st.secrets.get("users", {})
        return {str(user): str(password) for user, password in dict(users).items()}
    except Exception:
        return {}


def login_required() -> bool:
    users = configured_users()
    if not users:
        st.session_state.setdefault("username", "Operador local")
        st.caption("Modo local: el acceso con contraseña se activará al publicar la aplicación.")
        return True
    if st.session_state.get("authenticated"):
        top_left, top_right = st.columns([8, 1])
        top_left.caption(f"Sesión: {st.session_state['username']}")
        if top_right.button("Salir"):
            st.session_state.clear()
            st.rerun()
        return True
    st.title("📦 Karay Fill Rate")
    st.subheader("Iniciar sesión")
    with st.form("login"):
        username = st.text_input("Usuario")
        password = st.text_input("Contraseña", type="password")
        submitted = st.form_submit_button("Ingresar", type="primary", width="stretch")
    if submitted:
        expected = users.get(username)
        if expected is not None and hmac.compare_digest(password, expected):
            st.session_state["authenticated"] = True
            st.session_state["username"] = username
            st.rerun()
        st.error("Usuario o contraseña incorrectos.")
    return False


def main() -> None:
    st.set_page_config(page_title="Karay Fill Rate", page_icon="📦", layout="wide")
    if not login_required():
        return
    init_database()
    st.title("📦 Karay Fill Rate")
    process, history, forecast = st.tabs(["📦 Procesar pedidos", "🕘 Histórico", "📊 Indicadores"])
    with process:
        processing_tab()
    with history:
        history_tab()
    with forecast:
        forecast_tab()


if __name__ == "__main__":
    main()
