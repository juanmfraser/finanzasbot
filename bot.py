"""
FinanzasBot - Bot de Telegram para gestión de ingresos y gastos personales
Moneda base: ARS | Vista alternativa: USD (dólar blue)
"""

import os
import json
import logging
from datetime import datetime

import requests
import google.generativeai as genai
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

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config desde variables de entorno ──────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GOOGLE_SHEETS_ID = os.environ["GOOGLE_SHEETS_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
AUTHORIZED_USER_ID = os.environ.get("AUTHORIZED_USER_ID", "")

MESES = [
    "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]

CATEGORIAS_GASTO = [
    "Comida", "Transporte", "Entretenimiento", "Salud",
    "Hogar", "Ropa", "Educación", "Servicios", "Tecnología", "Otro",
]
CATEGORIAS_INGRESO = ["Sueldo", "Freelance", "Inversión", "Regalo", "Otro_Ingreso"]

# ── Gemini setup ───────────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-1.5-flash")

# ── Google Sheets ──────────────────────────────────────────────────────────
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
    try:
        ws = spreadsheet.worksheet("Presupuestos")
        records = ws.get_all_records()
        return {r["Categoría"]: float(r["Límite_Mensual"]) for r in records if r.get("Categoría")}
    except Exception:
        return {}

def set_budget(spreadsheet, category: str, limit: float):
    ws = spreadsheet.worksheet("Presupuestos")
    records = ws.get_all_records()
    for i, r in enumerate(records, start=2):
        if r.get("Categoría") == category:
            ws.update_cell(i, 2, limit)
            return
    ws.append_row([category, limit])

def get_monthly_records(spreadsheet, month: int, year: int) -> list:
    ws = spreadsheet.worksheet("Transacciones")
    records = ws.get_all_records()
    return [
        r for r in records
        if int(r.get("Mes", 0)) == month and int(r.get("Año", 0)) == year
    ]

# ── Tipo de cambio ─────────────────────────────────────────────────────────
def get_usd_rate() -> float | None:
    try:
        r = requests.get("https://dolarapi.com/v1/dolares/blue", timeout=5)
        data = r.json()
        return float(data["venta"])
    except Exception:
        pass
    try:
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=5)
        data = r.json()
        return float(data["rates"]["ARS"])
    except Exception:
        return None

# ── Categorización con Gemini ──────────────────────────────────────────────
def parse_transaction(text: str) -> dict:
    prompt = f"""Analiza este mensaje y extrae la información de una transacción financiera.

Mensaje: "{text}"

Responde SOLO con un JSON válido con estos campos:
- type: "gasto" o "ingreso"
- amount: número positivo (sin símbolo de moneda, usa punto como decimal)
- description: descripción breve (máximo 40 caracteres)
- category: exactamente una de estas:
  Gastos: Comida, Transporte, Entretenimiento, Salud, Hogar, Ropa, Educación, Servicios, Tecnología, Otro
  Ingresos: Sueldo, Freelance, Inversión, Regalo, Otro_Ingreso

Ejemplos:
"café 350" → {{"type":"gasto","amount":350,"description":"Café","category":"Comida"}}
"uber 1200" → {{"type":"gasto","amount":1200,"description":"Uber","category":"Transporte"}}
"netflix 5500" → {{"type":"gasto","amount":5500,"description":"Netflix","category":"Servicios"}}
"cobré el sueldo 180000" → {{"type":"ingreso","amount":180000,"description":"Sueldo","category":"Sueldo"}}
"freelance 50000" → {{"type":"ingreso","amount":50000,"description":"Freelance","category":"Freelance"}}

Si no puedes identificar un monto numérico, devuelve: {{"error":"No encontré el monto"}}

Responde ÚNICAMENTE con el JSON, sin texto adicional, sin bloques de código markdown."""

    try:
        response = gemini_model.generate_content(prompt)
        result = response.text.strip()
        if result.startswith("```"):
            lines = result.split("\n")
            result = "\n".join(lines[1:-1])
        return json.loads(result)
    except Exception as e:
        logger.error(f"Error en parse_transaction: {e}")
        return {"error": "No pude procesar el mensaje"}

# ── Alertas de presupuesto ─────────────────────────────────────────────────
def check_budget_alert(spreadsheet, category: str, new_amount: float) -> str | None:
    budgets = get_budgets(spreadsheet)
    if category not in budgets:
        return None
    limit = budgets[category]
    now = datetime.now()
    records = get_monthly_records(spreadsheet, now.month, now.year)
    spent = sum(
        float(r["Monto_ARS"]) for r in records
        if r.get("Tipo") == "gasto" and r.get("Categoría") == category
    ) + new_amount
    if spent > limit:
        over = spent - limit
        return f"¡Superaste el límite de {category}! Llevas ${spent:,.0f} de ${limit:,.0f} ARS (${over:,.0f} de exceso)"
    elif spent >= limit * 0.8:
        pct = int(spent / limit * 100)
        return f"Vas al {pct}% del presupuesto de {category} (${spent:,.0f} de ${limit:,.0f} ARS)"
    return None

# ── Resumen mensual ────────────────────────────────────────────────────────
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
            cat = r.get("Categoría", "Otro")
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

# ── Helpers ────────────────────────────────────────────────────────────────
def is_authorized(user_id: int) -> bool:
    if not AUTHORIZED_USER_ID:
        return True
    return str(user_id) == AUTHORIZED_USER_ID

# ── Handlers ───────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "💰 *FinanzasBot* — tu asistente de finanzas personales\n\n"
        "📉 *Registrar gasto* — solo mandame un mensaje:\n"
        " • `café 350`\n"
        " • `uber 1200`\n"
        " • `supermercado 8500`\n\n"
        "💵 *Registrar ingreso:*\n"
        " • `cobré el sueldo 180000`\n"
        " • `freelance 50000`\n\n"
        "📊 *Comandos:*\n"
        " /resumen — resumen del mes actual\n"
        " /graficas — gráfica de gastos\n"
        " /presupuesto — ver o fijar límites\n"
        " /ayuda — esta ayuda\n\n"
        "_Todos los montos en ARS. También muestro el equivalente en USD (dólar blue)._"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.message.from_user.id):
        await update.message.reply_text("❌ No autorizado.")
        return
    text = update.message.text.strip()
    await update.message.chat.send_action("typing")
    transaction = parse_transaction(text)
    if "error" in transaction:
        await update.message.reply_text(
            f"❌ {transaction['error']}\n\n"
            "Ejemplos válidos:\n"
            " • `café 350`\n"
            " • `taxi 2000`\n"
            " • `cobré sueldo 150000`",
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
        emoji = "💸" if transaction["type"] == "gasto" else "💰"
        usd_str = f" (~${amount_usd:,.2f} USD)" if amount_usd else ""
        tipo_str = "Gasto" if transaction["type"] == "gasto" else "Ingreso"
        reply = (
            f"{emoji} *{tipo_str} registrado*\n\n"
            f"📝 {transaction['description']}\n"
            f"🏷️ {transaction['category']}\n"
            f"💵 ${amount_ars:,.0f} ARS{usd_str}\n"
            f"📅 {now.strftime('%d/%m/%Y %H:%M')}"
        )
        if alert:
            reply += f"\n\n⚠️ {alert}"
        await update.message.reply_text(reply, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error guardando transacción: {e}")
        await update.message.reply_text(f"❌ Error al guardar: {e}")

async def cmd_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.message.from_user.id):
        return
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
            await update.message.reply_text(f"No hay transacciones en {MESES[month]} {year}.")
            return
        balance_emoji = "✅" if summary["balance"] >= 0 else "🔴"
        usd_gastos = f" (~${summary['total_gastos_usd']:,.0f} USD)" if summary["total_gastos_usd"] else ""
        usd_ingresos = f" (~${summary['total_ingresos_usd']:,.0f} USD)" if summary["total_ingresos_usd"] else ""
        usd_balance = f" (~${abs(summary['balance_usd']):,.0f} USD)" if summary["balance_usd"] else ""
        text = (
            f"📊 *Resumen {MESES[month]} {year}*\n"
            f"{'─' * 26}\n"
            f"💰 Ingresos: ${summary['total_ingresos']:>12,.0f} ARS{usd_ingresos}\n"
            f"💸 Gastos:   ${summary['total_gastos']:>12,.0f} ARS{usd_gastos}\n"
            f"{balance_emoji} Balance:  ${summary['balance']:>12,.0f} ARS{usd_balance}\n"
        )
        if summary["gastos_por_categoria"]:
            text += "\n📈 *Por categoría:*\n"
            sorted_cats = sorted(summary["gastos_por_categoria"].items(), key=lambda x: x[1], reverse=True)
            for cat, amount in sorted_cats:
                pct = (amount / summary["total_gastos"] * 100) if summary["total_gastos"] else 0
                text += f" • {cat}: ${amount:,.0f} ({pct:.0f}%)\n"
        budgets = get_budgets(spreadsheet)
        if budgets:
            text += "\n🎯 *Presupuestos del mes:*\n"
            for cat, limit in budgets.items():
                spent = summary["gastos_por_categoria"].get(cat, 0)
                pct = int(spent / limit * 100) if limit else 0
                bar = "█" * min(pct // 10, 10) + "░" * max(10 - pct // 10, 0)
                over = " ⚠️" if pct >= 80 else ""
                text += f" {cat[:10]:<10} {bar} {pct}%{over}\n"
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error en /resumen: {e}")
        await update.message.reply_text(f"❌ Error: {e}")

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
        sorted_cats = sorted(summary["gastos_por_categoria"].items(), key=lambda x: x[1], reverse=True)
        max_amount = sorted_cats[0][1]
        lines = [f"📊 Gastos {MESES[now.month]} {now.year}", ""]
        for cat, amount in sorted_cats:
            pct = int((amount / max_amount) * 14)
            bar = "█" * pct + "░" * (14 - pct)
            lines.append(f"{cat[:13]:<13} {bar} ${amount:,.0f}")
        lines.append("")
        lines.append(f"Total: ${summary['total_gastos']:,.0f} ARS")
        if summary["total_gastos_usd"]:
            lines.append(f" ~${summary['total_gastos_usd']:,.0f} USD (blue)")
        await update.message.reply_text("```\n" + "\n".join(lines) + "\n```", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error en /graficas: {e}")
        await update.message.reply_text(f"❌ Error: {e}")

async def cmd_presupuesto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.message.from_user.id):
        return
    args = context.args
    if not args:
        try:
            spreadsheet = get_spreadsheet()
            budgets = get_budgets(spreadsheet)
            if not budgets:
                await update.message.reply_text(
                    "No hay presupuestos configurados.\n\nFijar uno: `/presupuesto Comida 30000`",
                    parse_mode="Markdown",
                )
                return
            text = "🎯 *Presupuestos mensuales:*\n\n"
            for cat, limit in budgets.items():
                text += f" • {cat}: ${limit:,.0f} ARS\n"
            text += "\nModificar: `/presupuesto Comida 35000`"
            await update.message.reply_text(text, parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")
        return
    if len(args) < 2:
        await update.message.reply_text(
            "Uso: `/presupuesto CATEGORÍA MONTO`\n\nEjemplo: `/presupuesto Comida 30000`\n\n"
            f"Categorías válidas:\n{', '.join(CATEGORIAS_GASTO)}",
            parse_mode="Markdown",
        )
        return
    category = args[0].capitalize()
    if category not in CATEGORIAS_GASTO:
        await update.message.reply_text(
            f"❌ Categoría inválida: `{category}`\n\nVálidas: {', '.join(CATEGORIAS_GASTO)}",
            parse_mode="Markdown",
        )
        return
    try:
        limit = float(args[1].replace(",", "").replace(".", ""))
    except ValueError:
        await update.message.reply_text("❌ El monto debe ser un número. Ej: `30000`")
        return
    try:
        spreadsheet = get_spreadsheet()
        set_budget(spreadsheet, category, limit)
        await update.message.reply_text(
            f"✅ Presupuesto de *{category}* → ${limit:,.0f} ARS/mes",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

# ── Main ───────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ayuda", cmd_ayuda))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("graficas", cmd_graficas))
    app.add_handler(CommandHandler("presupuesto", cmd_presupuesto))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("FinanzasBot iniciado ✓")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
