"""
FinanzasBot - Bot de Telegram para gestiÃ³n de ingresos y gastos personales
Moneda base: ARS | Vista alternativa: USD (dÃ³lar blue)
"""

import os
import json
import logging
from datetime import datetime

import requests
import anthropic
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# âââ Logging âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# âââ Config desde variables de entorno âââââââââââââââââââââââââââââââââââââââ
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_SHEETS_ID = os.environ["GOOGLE_SHEETS_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
AUTHORIZED_USER_ID = os.environ.get("AUTHORIZED_USER_ID", "")  # Opcional pero recomendado

MESES = [
    "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]

CATEGORIAS_GASTO = [
    "Comida", "Transporte", "Entretenimiento", "Salud",
    "Hogar", "Ropa", "EducaciÃ³n", "Servicios", "TecnologÃ­a", "Otro",
]
CATEGORIAS_INGRESO = ["Sueldo", "Freelance", "InversiÃ³n", "Regalo", "Otro_Ingreso"]

# âââ Clientes âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# âââ Google Sheets ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
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


def append_transaction(spreadsheet, row: list):
    ws = spreadsheet.worksheet("Transacciones")
    ws.append_row(row, value_input_option="USER_ENTERED")


def get_budgets(spreadsheet) -> dict:
    """Devuelve {categoria: limite} para el mes."""
    try:
        ws = spreadsheet.worksheet("Presupuestos")
        records = ws.get_all_records()
        return {r["CategorÃ­a"]: float(r["LÃ­mite_Mensual"]) for r in records if r.get("CategorÃ­a")}
    except Exception:
        return {}


def set_budget(spreadsheet, category: str, limit: float):
    ws = spreadsheet.worksheet("Presupuestos")
    records = ws.get_all_records()
    for i, r in enumerate(records, start=2):
        if r.get("CategorÃ­a") == category:
            ws.update_cell(i, 2, limit)
            return
    ws.append_row([category, limit])


def get_monthly_records(spreadsheet, month: int, year: int) -> list:
    ws = spreadsheet.worksheet("Transacciones")
    records = ws.get_all_records()
    return [
        r for r in records
        if int(r.get("Mes", 0)) == month and int(r.get("AÃ±o", 0)) == year
    ]


# âââ Tipo de cambio âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def get_usd_rate() -> float | None:
    """Obtiene el tipo de cambio dÃ³lar blue (ARS por 1 USD)."""
    try:
        r = requests.get("https://dolarapi.com/v1/dolares/blue", timeout=5)
        data = r.json()
        return float(data["venta"])
    except Exception:
        pass
    # Fallback: tipo de cambio oficial via open.er-api
    try:
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=5)
        data = r.json()
        return float(data["rates"]["ARS"])
    except Exception:
        return None


# âââ CategorizaciÃ³n con Claude ââââââââââââââââââââââââââââââââââââââââââââââââ
def parse_transaction(text: str) -> dict:
    """
    Devuelve:
      {"type": "gasto"|"ingreso", "amount": float, "description": str, "category": str}
    o {"error": str}
    """
    prompt = f"""Analiza este mensaje y extrae la informaciÃ³n de una transacciÃ³n financiera.

Mensaje: "{text}"

Responde SOLO con un JSON vÃ¡lido con estos campos:
- type: "gasto" o "ingreso"
- amount: nÃºmero positivo (sin sÃ­mbolo de moneda, usa punto como decimal)
- description: descripciÃ³n breve (mÃ¡ximo 40 caracteres)
- category: exactamente una de estas:
    Gastos: Comida, Transporte, Entretenimiento, Salud, Hogar, Ropa, EducaciÃ³n, Servicios, TecnologÃ­a, Otro
    Ingresos: Sueldo, Freelance, InversiÃ³n, Regalo, Otro_Ingreso

Ejemplos:
"cafÃ© 350"              â {{"type":"gasto","amount":350,"description":"CafÃ©","category":"Comida"}}
"uber 1200"             â {{"type":"gasto","amount":1200,"description":"Uber","category":"Transporte"}}
"netflix 5500"          â {{"type":"gasto","amount":5500,"description":"Netflix","category":"Servicios"}}
"cobrÃ© el sueldo 180000"â {{"type":"ingreso","amount":180000,"description":"Sueldo","category":"Sueldo"}}
"freelance 50000"       â {{"type":"ingreso","amount":50000,"description":"Freelance","category":"Freelance"}}

Si no puedes identificar un monto numÃ©rico, devuelve: {{"error":"No encontrÃ© el monto"}}"""

    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        return json.loads(response.content[0].text.strip())
    except Exception:
        return {"error": "No pude procesar el mensaje"}


# âââ Alertas de presupuesto âââââââââââââââââââââââââââââââââââââââââââââââââââ
def check_budget_alert(spreadsheet, category: str, new_amount: float) -> str | None:
    budgets = get_budgets(spreadsheet)
    if category not in budgets:
        return None

    limit = budgets[category]
    now = datetime.now()
    records = get_monthly_records(spreadsheet, now.month, now.year)

    spent = sum(
        float(r["Monto_ARS"]) for r in records
        if r.get("Tipo") == "gasto" and r.get("CategorÃ­a") == category
    ) + new_amount

    if spent > limit:
        over = spent - limit
        return f"Â¡Superaste el lÃ­mite de {category}! Llevas ${spent:,.0f} de ${limit:,.0f} ARS (${over:,.0f} de exceso)"
    elif spent >= limit * 0.8:
        pct = int(spent / limit * 100)
        return f"Vas al {pct}% del presupuesto de {category} (${spent:,.0f} de ${limit:,.0f} ARS)"
    return None


# âââ Resumen mensual ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def build_monthly_summary(spreadsheet, month: int, year: int) -> dict | None:
    records = get_monthly_records(spreadsheet, month, year)
    if not records:
        return None

    gastos_cat = {}
    total_gastos = 0.0
    total_ingresos = 0.0
    total_gastos_usd = 0.0
    total_ingresos_usd = 0.0

    for r in records:
        amount_ars = float(r.get("Monto_ARS", 0))
        amount_usd = float(r.get("Monto_USD", 0) or 0)
        if r.get("Tipo") == "gasto":
            cat = r.get("CategorÃ­a", "Otro")
            gastos_cat[cat] = gastos_cat.get(cat, 0) + amount_ars
            total_gastos += amount_ars
            total_gastos_usd += amount_usd
        else:
            total_ingresos += amount_ars
            total_ingresos_usd += amount_usd

    return {
        "gastos_por_categoria": gastos_cat,
        "total_gastos": total_gastos,
        "total_ingresos": total_ingresos,
        "total_gastos_usd": total_gastos_usd,
        "total_ingresos_usd": total_ingresos_usd,
        "balance": total_ingresos - total_gastos,
        "balance_usd": total_ingresos_usd - total_gastos_usd,
    }


# âââ Helpers ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def is_authorized(user_id: int) -> bool:
    if not AUTHORIZED_USER_ID:
        return True
    return str(user_id) == AUTHORIZED_USER_ID


# âââ Handlers âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "ð *FinanzasBot* â tu asistente de finanzas personales\n\n"
        "ð *Registrar gasto* â solo mandame un mensaje:\n"
        "  â¢ `cafÃ© 350`\n"
        "  â¢ `uber 1200`\n"
        "  â¢ `supermercado 8500`\n\n"
        "ð° *Registrar ingreso:*\n"
        "  â¢ `cobrÃ© el sueldo 180000`\n"
        "  â¢ `freelance 50000`\n\n"
        "ð *Comandos:*\n"
        "  /resumen â resumen del mes actual\n"
        "  /graficas â grÃ¡fica de gastos\n"
        "  /presupuesto â ver o fijar lÃ­mites\n"
        "  /ayuda â esta ayuda\n\n"
        "_Todos los montos en ARS. TambiÃ©n muestro el equivalente en USD (dÃ³lar blue)._"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.message.from_user.id):
        await update.message.reply_text("â No autorizado.")
        return

    text = update.message.text.strip()
    await update.message.chat.send_action("typing")

    transaction = parse_transaction(text)

    if "error" in transaction:
        await update.message.reply_text(
            f"â {transaction['error']}\n\n"
            "Ejemplos vÃ¡lidos:\n"
            "  â¢ `cafÃ© 350`\n"
            "  â¢ `taxi 2000`\n"
            "  â¢ `cobrÃ© sueldo 150000`",
            parse_mode="Markdown",
        )
        return

    usd_rate = get_usd_rate()
    amount_ars = float(transaction["amount"])
    amount_usd = round(amount_ars / usd_rate, 2) if usd_rate else None

    now = datetime.now()
    row = [
        now.strftime("%Y-%m-%d %H:%M"),
        transaction["type"],
        transaction["description"],
        transaction["category"],
        amount_ars,
        usd_rate or "",
        amount_usd or "",
        now.month,
        now.year,
    ]

    try:
        spreadsheet = get_spreadsheet()
        append_transaction(spreadsheet, row)

        alert = check_budget_alert(spreadsheet, transaction["category"], amount_ars) \
            if transaction["type"] == "gasto" else None

        emoji = "ð¸" if transaction["type"] == "gasto" else "ð°"
        usd_str = f" (~${amount_usd:,.2f} USD)" if amount_usd else ""
        tipo_str = "Gasto" if transaction["type"] == "gasto" else "Ingreso"

        reply = (
            f"{emoji} *{tipo_str} registrado*\n\n"
            f"ð {transaction['description']}\n"
            f"ð·ï¸ {transaction['category']}\n"
            f"ðµ ${amount_ars:,.0f} ARS{usd_str}\n"
            f"ð {now.strftime('%d/%m/%Y %H:%M')}"
        )
        if alert:
            reply += f"\n\nâ ï¸ {alert}"

        await update.message.reply_text(reply, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Error guardando transacciÃ³n: {e}")
        await update.message.reply_text(f"â Error al guardar: {e}")


async def cmd_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.message.from_user.id):
        return

    # Parsear mes/aÃ±o opcionales: /resumen o /resumen 3 2026
    args = context.args
    now = datetime.now()
    try:
        month = int(args[0]) if args else now.month
        year = int(args[1]) if len(args) > 1 else now.year
    except (ValueError, IndexError):
        month, year = now.month, now.year

    await update.message.chat.send_action("typing")

    try:
        spreadsheet = get_spreadsheet()
        summary = build_monthly_summary(spreadsheet, month, year)

        if not summary:
            await update.message.reply_text(
                f"No hay transacciones en {MESES[month]} {year}."
            )
            return

        balance_emoji = "â" if summary["balance"] >= 0 else "ð´"
        usd_gastos = f" (~${summary['total_gastos_usd']:,.0f} USD)" if summary["total_gastos_usd"] else ""
        usd_ingresos = f" (~${summary['total_ingresos_usd']:,.0f} USD)" if summary["total_ingresos_usd"] else ""
        usd_balance = f" (~${abs(summary['balance_usd']):,.0f} USD)" if summary["balance_usd"] else ""

        text = (
            f"ð *Resumen {MESES[month]} {year}*\n"
            f"{'â' * 26}\n"
            f"ð° Ingresos:  ${summary['total_ingresos']:>12,.0f} ARS{usd_ingresos}\n"
            f"ð¸ Gastos:    ${summary['total_gastos']:>12,.0f} ARS{usd_gastos}\n"
            f"{balance_emoji} Balance:   ${summary['balance']:>12,.0f} ARS{usd_balance}\n"
        )

        if summary["gastos_por_categoria"]:
            text += "\nð *Por categorÃ­a:*\n"
            sorted_cats = sorted(
                summary["gastos_por_categoria"].items(),
                key=lambda x: x[1],
                reverse=True,
            )
            for cat, amount in sorted_cats:
                pct = (amount / summary["total_gastos"] * 100) if summary["total_gastos"] else 0
                text += f"  â¢ {cat}: ${amount:,.0f} ({pct:.0f}%)\n"

        budgets = get_budgets(spreadsheet)
        if budgets:
            text += "\nð¯ *Presupuestos del mes:*\n"
            for cat, limit in budgets.items():
                spent = summary["gastos_por_categoria"].get(cat, 0)
                pct = int(spent / limit * 100) if limit else 0
                bar = "â" * min(pct // 10, 10) + "â" * max(10 - pct // 10, 0)
                over = " â ï¸" if pct >= 80 else ""
                text += f"  {cat[:10]:<10} {bar} {pct}%{over}\n"

        await update.message.reply_text(text, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Error en /resumen: {e}")
        await update.message.reply_text(f"â Error: {e}")


async def cmd_graficas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.message.from_user.id):
        return

    await update.message.chat.send_action("typing")
    now = datetime.now()

    try:
        spreadsheet = get_spreadsheet()
        summary = build_monthly_summary(spreadsheet, now.month, now.year)

        if not summary or not summary["gastos_por_categoria"]:
            await update.message.reply_text("No hay gastos registrados este mes.")
            return

        sorted_cats = sorted(
            summary["gastos_por_categoria"].items(), key=lambda x: x[1], reverse=True
        )
        max_amount = sorted_cats[0][1]

        lines = [f"ð Gastos {MESES[now.month]} {now.year}", ""]
        for cat, amount in sorted_cats:
            pct = int((amount / max_amount) * 14)
            bar = "â" * pct + "â" * (14 - pct)
            usd = f" ~${amount / summary.get('total_gastos_usd', 1):.0f}" if False else ""
            lines.append(f"{cat[:13]:<13} {bar}  ${amount:,.0f}")

        lines.append("")
        lines.append(f"Total: ${summary['total_gastos']:,.0f} ARS")
        if summary["total_gastos_usd"]:
            lines.append(f"       ~${summary['total_gastos_usd']:,.0f} USD (blue)")

        await update.message.reply_text(
            "```\n" + "\n".join(lines) + "\n```", parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Error en /graficas: {e}")
        await update.message.reply_text(f"â Error: {e}")


async def cmd_presupuesto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.message.from_user.id):
        return

    args = context.args

    # Sin args â mostrar presupuestos actuales
    if not args:
        try:
            spreadsheet = get_spreadsheet()
            budgets = get_budgets(spreadsheet)
            if not budgets:
                await update.message.reply_text(
                    "No hay presupuestos configurados.\n\n"
                    "Fijar uno: `/presupuesto Comida 30000`",
                    parse_mode="Markdown",
                )
                return
            text = "ð¯ *Presupuestos mensuales:*\n\n"
            for cat, limit in budgets.items():
                text += f"  â¢ {cat}: ${limit:,.0f} ARS\n"
            text += "\nModificar: `/presupuesto Comida 35000`"
            await update.message.reply_text(text, parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"â Error: {e}")
        return

    if len(args) < 2:
        await update.message.reply_text(
            "Uso: `/presupuesto CATEGORÃA MONTO`\n\n"
            "Ejemplo: `/presupuesto Comida 30000`\n\n"
            f"CategorÃ­as vÃ¡lidas:\n{', '.join(CATEGORIAS_GASTO)}",
            parse_mode="Markdown",
        )
        return

    category = args[0].capitalize()
    if category not in CATEGORIAS_GASTO:
        await update.message.reply_text(
            f"â CategorÃ­a invÃ¡lida: `{category}`\n\n"
            f"VÃ¡lidas: {', '.join(CATEGORIAS_GASTO)}",
            parse_mode="Markdown",
        )
        return

    try:
        limit = float(args[1].replace(",", "").replace(".", ""))
    except ValueError:
        await update.message.reply_text("â El monto debe ser un nÃºmero. Ej: `30000`")
        return

    try:
        spreadsheet = get_spreadsheet()
        set_budget(spreadsheet, category, limit)
        await update.message.reply_text(
            f"â Presupuesto de *{category}* â ${limit:,.0f} ARS/mes",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"â Error: {e}")


# âââ Main âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ayuda", cmd_ayuda))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("graficas", cmd_graficas))
    app.add_handler(CommandHandler("presupuesto", cmd_presupuesto))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("FinanzasBot iniciado â")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
