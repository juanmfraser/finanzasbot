"""
setup_sheet.py â Inicializa la Google Sheet con las hojas y encabezados necesarios.
Ejecutar UNA sola vez antes de arrancar el bot.

Uso:
    python setup_sheet.py
"""

import os
import json
import gspread
from google.oauth2.service_account import Credentials

GOOGLE_SHEETS_ID = os.environ["GOOGLE_SHEETS_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]


def get_spreadsheet():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=[
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(GOOGLE_SHEETS_ID)


def setup():
    print("Conectando a Google Sheets...")
    spreadsheet = get_spreadsheet()
    existing = [ws.title for ws in spreadsheet.worksheets()]

    # ââ Hoja: Transacciones ââââââââââââââââââââââââââââââââââââââââââââââââ
    TRANS_HEADERS = [
        "Fecha", "Tipo", "DescripciÃ³n", "CategorÃ­a",
        "Monto_ARS", "Tipo_Cambio_USD", "Monto_USD", "Mes", "AÃ±o",
    ]
    if "Transacciones" not in existing:
        ws = spreadsheet.add_worksheet("Transacciones", rows=1000, cols=10)
        print("  â Hoja 'Transacciones' creada")
    else:
        ws = spreadsheet.worksheet("Transacciones")
        print("  â¹ï¸  Hoja 'Transacciones' ya existe")

    # Escribir encabezados si la hoja estÃ¡ vacÃ­a
    if not ws.get_all_values():
        ws.append_row(TRANS_HEADERS)
        print("     Encabezados escritos")

    # ââ Hoja: Presupuestos âââââââââââââââââââââââââââââââââââââââââââââââââ
    BUDGET_HEADERS = ["CategorÃ­a", "LÃ­mite_Mensual"]
    if "Presupuestos" not in existing:
        ws2 = spreadsheet.add_worksheet("Presupuestos", rows=50, cols=3)
        print("  â Hoja 'Presupuestos' creada")
    else:
        ws2 = spreadsheet.worksheet("Presupuestos")
        print("  â¹ï¸  Hoja 'Presupuestos' ya existe")

    if not ws2.get_all_values():
        ws2.append_row(BUDGET_HEADERS)
        print("     Encabezados escritos")

    # ââ Eliminar hoja por defecto si estÃ¡ vacÃ­a ââââââââââââââââââââââââââââ
    try:
        default = spreadsheet.worksheet("Sheet1")
        if not default.get_all_values():
            spreadsheet.del_worksheet(default)
            print("  ðï¸  Hoja 'Sheet1' vacÃ­a eliminada")
    except Exception:
        pass  # No existe o no se puede eliminar

    print("\nâ Google Sheet configurada correctamente.")
    print(f"   URL: https://docs.google.com/spreadsheets/d/{GOOGLE_SHEETS_ID}")


if __name__ == "__main__":
    setup()
