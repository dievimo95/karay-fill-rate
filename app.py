from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import pandas as pd
import pdfplumber
import streamlit as st
from sqlalchemy import (
    Column, Date as SQLDate, DateTime, Float, ForeignKey, Index, Integer,
    MetaData, String, Table, Text, UniqueConstraint, create_engine, delete,
    insert, select, update,
)


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "fillrate.db"

metadata = MetaData()
cargas_table = Table(
    "cargas", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("fecha", DateTime, nullable=False),
    Column("cliente", String(200), nullable=False, default=""),
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
    Column("periodo", String(7), nullable=False, unique=True),
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
facturas_acumuladas_table = Table(
    "facturas_acumuladas", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("archivo_hash", String(64), nullable=False),
    Column("archivo", String(255), nullable=False),
    Column("linea", Integer, nullable=False),
    Column("fecha_documento", SQLDate, nullable=False),
    Column("codigo_interno", String(20), nullable=False, default=""),
    Column("ean", String(20), nullable=False, default=""),
    Column("producto", Text, nullable=False, default=""),
    Column("unidades", Float, nullable=False),
    UniqueConstraint("archivo_hash", "linea", name="uq_factura_archivo_linea"),
)
Index("idx_forecast_periodo", forecast_table.c.periodo)
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
        r"\bOC\s*:\s*((?:[A-Z]{1,3}\s*)?\d(?:[A-Z0-9 ]{3,18}\d))",
        text,
        re.I,
    )
    if explicit:
        return re.sub(r"\s+", "", explicit.group(1)).upper()
    return find_context(text)[0]


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
        for cells in table[header_index + 1:]:
            if not cells or not re.fullmatch(r"\d+(?:[.,]\d+)?", str(cells[0] or "").strip()):
                continue
            quantity = normalize_number(cells[0])
            client_code = re.sub(r"\D", "", str(cells[10] or "")) if len(cells) > 10 else ""
            product = str(cells[6] or "").replace("\n", " ").strip() if len(cells) > 6 else ""
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
                "precio": normalize_number(cells[11] if len(cells) > 11 else 0),
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


def save_invoice_ledger(invoice_rows: pd.DataFrame) -> None:
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
                        facturas_acumuladas_table.c.archivo.like(f"%{sequence}%")
                    ))
        for file_hash in invoice_rows["archivo_hash"].dropna().unique():
            db.execute(delete(facturas_acumuladas_table).where(
                facturas_acumuladas_table.c.archivo_hash == str(file_hash)
            ))
        records = []
        for _, row in invoice_rows.iterrows():
            records.append({
                "archivo_hash": str(row.get("archivo_hash", "")),
                "archivo": str(row.get("archivo_nombre", "")),
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


def save_forecast(period: str, uploaded_file, forecast_rows: pd.DataFrame) -> int:
    with get_engine().begin() as db:
        existing = db.execute(select(forecast_table.c.id).where(forecast_table.c.periodo == period)).first()
        if existing:
            forecast_id = int(existing.id)
            db.execute(update(forecast_table).where(forecast_table.c.id == forecast_id).values(
                archivo=uploaded_file.name, actualizado=datetime.now()
            ))
            db.execute(delete(forecast_detalle_table).where(forecast_detalle_table.c.forecast_id == forecast_id))
        else:
            created = db.execute(insert(forecast_table).values(
                periodo=period, archivo=uploaded_file.name, actualizado=datetime.now()
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

    def normalize_internal(value: object) -> str:
        digits = re.sub(r"\D", "", str(value or ""))
        return digits.zfill(8) if digits and len(digits) <= 8 else digits

    for frame in (orders, invoices):
        frame["codigo"] = frame["codigo"].map(clean_code)
        frame["codigo_interno"] = frame["codigo_interno"].map(normalize_internal)

    # The invoice normally contains both identifiers. Use it as a homologation
    # table so an order carrying only EAN can still match by internal code.
    invoice_codes = invoices[
        (invoices["codigo"] != "") & (invoices["codigo_interno"] != "")
    ].drop_duplicates("codigo")
    ean_to_internal = dict(zip(invoice_codes["codigo"], invoice_codes["codigo_interno"]))
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

    def match_key(row: pd.Series) -> str:
        identity = row.codigo_interno or row.codigo
        return f"{row.oc}|{identity}" if row.oc else identity

    orders["match_key"] = orders.apply(match_key, axis=1)
    invoices["match_key"] = invoices.apply(match_key, axis=1)
    order_group = orders.groupby("match_key", as_index=False).agg(
        oc=("oc", "first"), codigo=("codigo", "first"), producto=("producto", "first"),
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
    detail = result[DETAIL_COLUMNS].sort_values(["oc", "producto"]).reset_index(drop=True)
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


def file_fingerprint(order_files, invoice_files) -> str:
    digest = hashlib.sha256()
    for role, files in (("PEDIDO", order_files), ("FACTURA", invoice_files)):
        digest.update(role.encode("ascii"))
        for uploaded in sorted(files, key=lambda f: f.name):
            uploaded.seek(0)
            digest.update(uploaded.name.encode("utf-8"))
            digest.update(uploaded.read())
            uploaded.seek(0)
    return digest.hexdigest()


def save_run(detail: pd.DataFrame, summary: dict, cliente: str, usuario: str, order_files, invoice_files) -> tuple[int, bool]:
    fingerprint = file_fingerprint(order_files, invoice_files)
    with get_engine().begin() as db:
        existing = db.execute(select(cargas_table.c.id).where(cargas_table.c.fingerprint == fingerprint)).first()
        values = dict(
            fecha=datetime.now(), cliente=cliente, usuario=usuario,
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

    if st.button("Procesar y guardar", type="primary", width="stretch"):
        if not order_files or not invoice_files:
            st.error("Selecciona al menos un pedido y una factura PDF.")
            return
        with st.spinner("Leyendo y conciliando los documentos..."):
            order_rows, order_clients, order_warnings = parse_files(order_files, "pedido")
            invoice_rows, invoice_clients, invoice_warnings = parse_files(invoice_files, "factura")
            save_invoice_ledger(invoice_rows)
            try:
                detail = reconcile(order_rows, invoice_rows)
            except ValueError as exc:
                st.error(str(exc))
                for warning in order_warnings + invoice_warnings:
                    st.warning(warning)
                return
            summary = totals(detail)
            detected_client = cliente.strip() or next(iter(order_clients + invoice_clients), "Sin especificar")
            run_id, created = save_run(detail, summary, detected_client, usuario.strip() or "Operador", order_files, invoice_files)
            st.session_state["last_result"] = (detail, summary)
        if created:
            st.success(f"Procesamiento #{run_id} guardado correctamente en el Histórico.")
        else:
            st.info(f"El procesamiento #{run_id} fue recalculado y actualizado sin crear un duplicado.")
        for warning in order_warnings + invoice_warnings:
            st.warning(warning)

    if "last_result" in st.session_state:
        detail, summary = st.session_state["last_result"]
        show_metrics(summary)
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
    c1, c2, c3 = st.columns(3)
    start = c1.date_input("Desde", value=history["fecha_dt"].dt.date.min())
    end = c2.date_input("Hasta", value=date.today())
    clients = sorted(x for x in history["cliente"].dropna().unique() if x)
    client = c3.selectbox("Cliente", ["Todos", *clients])
    filtered = history[(history["fecha_dt"].dt.date >= start) & (history["fecha_dt"].dt.date <= end)]
    if client != "Todos":
        filtered = filtered[filtered["cliente"] == client]
    display = filtered[["id", "fecha_dt", "cliente", "usuario", "fill_rate", "venta_perdida", "pedidos_archivos", "facturas_archivos"]].copy()
    display.columns = ["ID", "Fecha", "Cliente", "Usuario", "Fill Rate", "Venta perdida", "Pedidos", "Facturas"]
    st.dataframe(display, width="stretch", hide_index=True, column_config={
        "Fecha": st.column_config.DatetimeColumn(format="DD/MM/YYYY HH:mm"),
        "Fill Rate": st.column_config.NumberColumn(format="%.2f%%"),
        "Venta perdida": st.column_config.NumberColumn(format="$%.2f"),
    })
    if filtered.empty:
        return
    selected = st.selectbox("Ver detalle del procesamiento", filtered["id"].tolist(), format_func=lambda x: f"Procesamiento #{x}")
    detail_columns = [detalle_table.c[c] for c in DETAIL_COLUMNS]
    with get_engine().connect() as db:
        detail = pd.read_sql(
            select(*detail_columns).where(detalle_table.c.carga_id == int(selected)).order_by(detalle_table.c.oc, detalle_table.c.producto),
            db,
        )
    st.dataframe(detail, width="stretch", hide_index=True)
    st.download_button("Descargar este detalle", detail.to_csv(index=False).encode("utf-8-sig"), f"fill_rate_{selected}.csv", "text/csv")


def forecast_tab() -> None:
    st.subheader("Fill Rate Interno")
    st.write("Carga el forecast del mes y compáralo con todas las facturas procesadas, sin duplicar archivos.")
    upload_left, upload_right = st.columns([2, 1])
    forecast_file = upload_left.file_uploader(
        "Forecast Excel, CSV o TXT", type=["xlsx", "xls", "csv", "txt"], key="forecast_file"
    )
    month_value = upload_right.date_input("Mes del forecast", value=date.today().replace(day=1), key="forecast_month")
    if st.button("Guardar o reemplazar forecast", type="primary", width="stretch"):
        if forecast_file is None:
            st.error("Selecciona el archivo del forecast.")
        else:
            try:
                forecast_rows = forecast_dataframe(forecast_file)
                period = month_value.strftime("%Y-%m")
                save_forecast(period, forecast_file, forecast_rows)
                st.success(
                    f"Forecast {period} guardado: {len(forecast_rows)} productos y "
                    f"{forecast_rows['unidades'].sum():,.1f} unidades."
                )
            except Exception as exc:
                st.error(f"No se pudo cargar el forecast: {exc}")

    with get_engine().connect() as db:
        available = pd.read_sql(select(forecast_table).order_by(forecast_table.c.periodo.desc()), db)
    if available.empty:
        st.info("Carga el primer forecast para ver el avance mensual.")
        return

    selected_period = st.selectbox("Periodo", available["periodo"].tolist(), key="forecast_period")
    forecast_id = int(available.loc[available["periodo"] == selected_period, "id"].iloc[0])
    year, month = map(int, selected_period.split("-"))
    period_start = date(year, month, 1)
    period_end = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    with get_engine().connect() as db:
        forecast_rows = pd.read_sql(
            select(
                forecast_detalle_table.c.codigo_interno,
                forecast_detalle_table.c.ean,
                forecast_detalle_table.c.producto,
                forecast_detalle_table.c.unidades.label("forecast"),
            ).where(forecast_detalle_table.c.forecast_id == forecast_id), db,
        )
        invoices = pd.read_sql(
            select(
                facturas_acumuladas_table.c.codigo_interno,
                facturas_acumuladas_table.c.ean,
                facturas_acumuladas_table.c.producto,
                facturas_acumuladas_table.c.unidades,
            ).where(
                facturas_acumuladas_table.c.fecha_documento >= period_start,
                facturas_acumuladas_table.c.fecha_documento < period_end,
            ), db,
        )
    if invoices.empty:
        billed = pd.DataFrame(columns=["codigo_interno", "facturado"])
    else:
        billed = invoices.groupby("codigo_interno", as_index=False)["unidades"].sum().rename(
            columns={"unidades": "facturado"}
        )
    report = forecast_rows.merge(billed, on="codigo_interno", how="left")
    report["facturado"] = report["facturado"].fillna(0.0)
    report["pendiente"] = (report["forecast"] - report["facturado"]).clip(lower=0)
    report["excedente"] = (report["facturado"] - report["forecast"]).clip(lower=0)
    report["cumplimiento"] = report.apply(
        lambda row: 100 * row.facturado / row.forecast if row.forecast > 0 else (100 if row.facturado > 0 else 0),
        axis=1,
    )
    forecast_total = float(report["forecast"].sum())
    billed_total = float(report["facturado"].sum())
    pending_total = float(report["pendiente"].sum())
    achievement = 100 * billed_total / forecast_total if forecast_total else 0.0
    unmatched_units = 0.0
    if not invoices.empty:
        forecast_codes = set(report["codigo_interno"])
        unmatched_units = float(invoices.loc[~invoices["codigo_interno"].isin(forecast_codes), "unidades"].sum())

    metrics = st.columns(5)
    metrics[0].metric("Forecast", f"{forecast_total:,.1f}")
    metrics[1].metric("Facturado acumulado", f"{billed_total:,.1f}")
    metrics[2].metric("Pendiente", f"{pending_total:,.1f}")
    metrics[3].metric("Cumplimiento", f"{achievement:.1f}%")
    metrics[4].metric("Sin coincidencia", f"{unmatched_units:,.1f}")
    st.progress(min(max(achievement / 100, 0.0), 1.0), text=f"Avance de {selected_period}: {achievement:.1f}%")
    if invoices.empty:
        st.warning("Aún no hay facturas acumuladas para este mes. Vuelve a procesarlas una vez para alimentar el forecast.")
    if unmatched_units:
        st.warning(f"Hay {unmatched_units:,.1f} unidades facturadas cuyos códigos no aparecen en este forecast.")

    status = st.selectbox("Mostrar", ["Todos", "Pendientes", "Cumplidos", "Sobrecumplidos"])
    visible = report.copy()
    if status == "Pendientes":
        visible = visible[visible["pendiente"] > 0]
    elif status == "Cumplidos":
        visible = visible[(visible["pendiente"] == 0) & (visible["excedente"] == 0)]
    elif status == "Sobrecumplidos":
        visible = visible[visible["excedente"] > 0]
    display_columns = [
        "codigo_interno", "producto", "forecast", "facturado", "pendiente", "excedente", "cumplimiento"
    ]
    st.dataframe(visible[display_columns].sort_values("pendiente", ascending=False), width="stretch", hide_index=True,
        column_config={
            "codigo_interno": "COD",
            "producto": "Producto",
            "forecast": st.column_config.NumberColumn("Forecast", format="%.1f"),
            "facturado": st.column_config.NumberColumn("Facturado", format="%.1f"),
            "pendiente": st.column_config.NumberColumn("Pendiente", format="%.1f"),
            "excedente": st.column_config.NumberColumn("Excedente", format="%.1f"),
            "cumplimiento": st.column_config.NumberColumn("Cumplimiento", format="%.1f%%"),
        })
    chart = report.nlargest(15, "pendiente")[["producto", "pendiente"]].set_index("producto")
    if not chart.empty and chart["pendiente"].sum() > 0:
        st.caption("15 productos con mayor saldo pendiente")
        st.bar_chart(chart)
    st.download_button(
        "Descargar avance CSV", report[display_columns].to_csv(index=False).encode("utf-8-sig"),
        f"forecast_{selected_period}.csv", "text/csv",
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
    process, history, forecast = st.tabs(["📤 Procesar", "📚 Histórico", "📈 Fill Rate Interno"])
    with process:
        processing_tab()
    with history:
        history_tab()
    with forecast:
        forecast_tab()


if __name__ == "__main__":
    main()
