import unittest

import pandas as pd

from app import odoo_invoice_rows, odoo_order_rows, reconcile


ENGLISH_ORDER = """SOCIEDAD CIVIL Y COMERCIAL ZICO
MARCSEAL S.A.
Order # S04754
Order Date: Salesperson:
08/24/2026 María del Cisne Carrión
DESCRIPTION QUANTITY UNIT PRICE DISC.% TAXES AMOUNT
[01100011] Aceite de Ajonjolí Extra Virgen ZICO 17Kg 1.0000 Units 239.70000 10.00000 IVA 0% $ 215.73
Subtotal sin impuestos $ 215.73
OC: 120000195
"""

ENGLISH_INVOICE = """FACTURA
No.: 001-100-000003035
Partner: Emission Date: 08/24/2026
MARCSEAL S.A. Source: S04754
PRINCIPAL DESCRIPTION QUANTITY UNIT DISC.% ADDITIONAL DETAILS TAXES AMOUNT
CODE PRICE
01100011 [01100011] Aceite de Ajonjolí Extra Virgen ZICO 17Kg 1.0000 239.70000 10.00000 IVA 0% $ 215.73
PAYMENT METHODS
Total $ 215.73
OC: 120000195
Page: 1 / 1
"""

SPANISH_ORDER = """Orden # S04899
Fecha de pedido: 24/08/2026
DESCRIPCIÓN CANTIDAD PRECIO UNITARIO
[01100011] Aceite de Ajonjolí Extra Virgen ZICO 17Kg 2,0000 Unidades 239,70000
Base imponible $ 479.40
"""


class OdooParserTests(unittest.TestCase):
    def test_english_odoo_order_uses_customer_oc(self):
        rows = odoo_order_rows(ENGLISH_ORDER)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["oc"], "120000195")
        self.assertEqual(rows[0]["codigo_interno"], "01100011")
        self.assertEqual(rows[0]["cantidad"], 1.0)
        self.assertEqual(rows[0]["precio"], 239.7)

    def test_english_odoo_invoice_reads_dot_decimals(self):
        rows = odoo_invoice_rows(ENGLISH_INVOICE, "120000195")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["codigo_interno"], "01100011")
        self.assertEqual(rows[0]["cantidad"], 1.0)
        self.assertEqual(rows[0]["precio"], 239.7)
        self.assertEqual(rows[0]["total_documento"], 215.73)
        self.assertEqual(rows[0]["fecha_documento"].isoformat(), "2026-08-24")

    def test_s04754_reconciles_at_100_percent(self):
        orders = pd.DataFrame(odoo_order_rows(ENGLISH_ORDER))
        orders["cliente"] = "MARCSEAL S.A."
        invoices = pd.DataFrame(odoo_invoice_rows(ENGLISH_INVOICE, "120000195"))

        detail = reconcile(orders, invoices)

        self.assertEqual(len(detail), 1)
        self.assertEqual(detail.iloc[0].pedidas, 1.0)
        self.assertEqual(detail.iloc[0].facturadas, 1.0)
        self.assertEqual(detail.iloc[0].fill_rate, 100.0)

    def test_existing_spanish_odoo_format_remains_supported(self):
        rows = odoo_order_rows(SPANISH_ORDER)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["oc"], "S04899")
        self.assertEqual(rows[0]["cantidad"], 2.0)
        self.assertEqual(rows[0]["precio"], 239.7)


if __name__ == "__main__":
    unittest.main()
