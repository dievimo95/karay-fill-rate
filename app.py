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
    Column, DateTime, Float, ForeignKey, Index, Integer, MetaData, String, Table,
    Text, create_engine, delete, insert, select, update,
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
    return "\n".join(texts), tables


def find_context(text: str) -> tuple[str, str]:
    oc_patterns = [r"(?:orden\s+de\s+compra|orden|pedido|o\.?c\.?)\s*[:#Nº°-]*\s*((?:\d[ ]*){6,20})"]
    client_patterns = [r"(?:cliente|raz[oó]n\s+social)\s*[:\-]\s*([^\n]{3,80})"]
    oc = next((re.sub(r"\D", "", m.group(1)) for p in oc_patterns if (m := re.search(p, text, re.I))), "")
    cliente = next((m.group(1).strip() for p in client_patterns if (m := re.search(p, text, re.I))), "")
    if not cliente and re.search(r"corporaci[oó]n\s+favorita|supermaxi", text, re.I):
        cliente = "Corporación Favorita"
    return oc, cliente


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
                "codigo": match.group("ean"),
                "producto": re.sub(r"\s+", " ", match.group("product")).strip(),
                "cantidad": cases * units_per_case,
                "precio": normalize_number(match.group("price")),
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
    rows = []
    for match in row_pattern.finditer(text):
        body = re.sub(r"\s+", " ", match.group("body")).strip()
        amounts = qty_price.search(body)
        eans = re.findall(r"(?<!\d)(\d{13})(?!\d)", body)
        if not amounts or not eans:
            continue
        description = body[:amounts.start()].strip(" -")
        description = re.sub(r"\s*-?\s*Ref\.?\s*$", "", description, flags=re.I)
        rows.append({
            "oc": default_oc,
            "codigo": eans[-1],
            "producto": description,
            "cantidad": normalize_number(amounts.group("qty")),
            "precio": normalize_number(amounts.group("price")),
            "tipo": "factura",
        })
    # Keep the official document total once per invoice. This lets the UI show
    # both the taxable/base sale and the amount including VAT without counting
    # the same invoice total once for every product line.
    total_matches = re.findall(r"^Total\s+\$\s*([\d.,]+)\s*$", text, re.I | re.M)
    if rows and total_matches:
        rows[0]["total_documento"] = normalize_number(total_matches[-1])
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
            if client:
                clients.append(client)
            if document_type == "pedido":
                rows = favorita_order_rows(text)
            else:
                rows = odoo_invoice_rows(text, oc)
            rows = rows or table_rows(tables, document_type, oc) or text_rows(text, document_type, oc)
            if not rows:
                warnings.append(f"{uploaded.name}: no se reconocieron líneas de productos.")
            all_rows.extend(rows)
        except Exception as exc:
            warnings.append(f"{uploaded.name}: no se pudo leer ({exc}).")
    return pd.DataFrame(all_rows), clients, warnings


def reconcile(order_rows: pd.DataFrame, invoice_rows: pd.DataFrame) -> pd.DataFrame:
    if order_rows.empty:
        raise ValueError("No se encontraron líneas válidas en los pedidos.")
    orders = order_rows.copy()
    invoices = invoice_rows.copy()
    for frame in (orders, invoices):
        for column in ("oc", "codigo", "producto"):
            if column not in frame:
                frame[column] = ""
            frame[column] = frame[column].fillna("").astype(str).str.strip()
        if "cantidad" not in frame:
            frame["cantidad"] = 0.0
        if "precio" not in frame:
            frame["precio"] = 0.0

    # OC + code is preferred. If a PDF has no OC, code remains a safe common key.
    orders["match_key"] = orders.apply(lambda r: f"{r.oc}|{r.codigo}" if r.oc else r.codigo, axis=1)
    invoices["match_key"] = invoices.apply(lambda r: f"{r.oc}|{r.codigo}" if r.oc else r.codigo, axis=1)
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
    order_files = left.file_uploader("Pedidos PDF", type=["pdf"], accept_multiple_files=True, key="orders")
    invoice_files = right.file_uploader("Facturas PDF", type=["pdf"], accept_multiple_files=True, key="invoices")
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
    process, history = st.tabs(["📤 Procesar", "📚 Histórico"])
    with process:
        processing_tab()
    with history:
        history_tab()


if __name__ == "__main__":
    main()
