import sys
import logging
import os
import asyncio
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Final
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    PicklePersistence,
)

from sources.vinted import (
    fetch as fetch_vinted,
    get_last_stats as get_vinted_stats,
    set_category,
    get_current_category_label,
)
from pipeline.normalize import normalize_listing, USE_ANTHROPIC_NORMALIZER
import pipeline.normalize as normalize_module
from pipeline.estimate import estimate_listing
from storage.db import insert_listing, get_recent_sent, get_connection


# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    stream=sys.stdout,
)
LOGGER = logging.getLogger(__name__)

TOKEN: Final[str | None] = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID: Final[str | None] = os.getenv("TELEGRAM_CHAT_ID")
PERSISTENCE_FILE: Final[str] = os.getenv("PERSISTENCE_FILE", "/tmp/vinted_bot_data.pickle")

DEFAULT_SETTINGS: Final[dict[str, Decimal]] = {
    "min_profit_eur": Decimal("30"),
    "min_roi": Decimal("25"),
    "default_fee_percent": Decimal("0"),
    "default_risk_percent": Decimal("5"),
    "safety_resale_percent": Decimal("90"),
}

MONEY_STEP: Final[Decimal] = Decimal("0.01")
PERCENT_STEP: Final[Decimal] = Decimal("0.1")

# Vinted n'offre pas de webhook temps réel : un scan très rapproché
# (60s) est l'équivalent le plus proche d'une notification instantanée
# dès qu'une annonce est mise en ligne.
CYCLE_SECONDS: Final[int] = int(os.getenv("CYCLE_SECONDS", "60"))
SOURCES = [fetch_vinted]
SOURCE_STATS_GETTERS = {fetch_vinted: get_vinted_stats}

SCAN_LOCK = asyncio.Lock()

BOT_STATE = {
    "last_found": 0,
    "last_sent": 0,
    "last_duplicates": 0,
    "last_error": None,
}


# ---------------------------------------------------------
# OUTILS CALCULATEUR (commandes conservées, plus dans le menu)
# ---------------------------------------------------------

def parse_decimal(value: str) -> Decimal:
    cleaned = (
        value.strip().replace("€", "").replace("%", "")
        .replace(" ", "").replace(",", ".")
    )
    try:
        number = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError("Nombre invalide.") from exc
    if not number.is_finite():
        raise ValueError("Nombre invalide.")
    if number < 0:
        raise ValueError("Les nombres doivent être positifs.")
    return number


def money(value: Decimal) -> str:
    rounded = value.quantize(MONEY_STEP, rounding=ROUND_HALF_UP)
    return f"{rounded} €"


def percent(value: Decimal) -> str:
    return f"{value.quantize(PERCENT_STEP, rounding=ROUND_HALF_UP)} %"


def get_settings(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Decimal]:
    settings = context.user_data.setdefault("settings", DEFAULT_SETTINGS.copy())
    for key, value in DEFAULT_SETTINGS.items():
        settings.setdefault(key, value)
    return settings


async def check_access(update: Update) -> bool:
    chat = update.effective_chat
    if not chat:
        return False
    if ALLOWED_CHAT_ID and str(chat.id) != str(ALLOWED_CHAT_ID):
        if update.effective_message:
            await update.effective_message.reply_text("⛔ Accès non autorisé.")
        return False
    return True


def calculate_score(profit, roi, purchase_roi, target, risk_cost, total_cost) -> int:
    score = Decimal("0")
    if target > 0:
        margin_ratio = profit / target
        score += min(Decimal("35"), margin_ratio * Decimal("20"))
    score += min(Decimal("30"), max(Decimal("0"), roi) * Decimal("0.6"))
    score += min(Decimal("20"), max(Decimal("0"), purchase_roi) * Decimal("0.25"))
    if total_cost > 0:
        risk_ratio = risk_cost / total_cost * Decimal("100")
        if risk_ratio <= 3:
            score += 15
        elif risk_ratio <= 7:
            score += 10
        elif risk_ratio <= 12:
            score += 5
    return max(0, min(100, int(score)))


def verdict_for(profit, roi, safe_profit, target, min_roi) -> str:
    if profit <= 0:
        return "❌ À ÉVITER"
    if safe_profit <= 0:
        return "⚠️ RENTABLE SEULEMENT DANS LE MEILLEUR CAS"
    if profit >= target * Decimal("2") and roi >= min_roi * Decimal("2"):
        return "🔥 EXCELLENTE AFFAIRE"
    if profit >= target * Decimal("1.5") and roi >= min_roi * Decimal("1.5"):
        return "🟢 TRÈS BONNE AFFAIRE"
    if profit >= target and roi >= min_roi:
        return "✅ RENTABLE"
    if profit >= target and roi < min_roi:
        return "⚠️ BONNE MARGE, RENDEMENT MOYEN"
    if profit >= target * Decimal("0.6") and roi >= min_roi:
        return "🟡 PETITE AFFAIRE RENTABLE"
    return "⚠️ MARGE TROP FAIBLE"


def analysis_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📘 Aide", callback_data="help_quick"),
         InlineKeyboardButton("⚙️ Réglages", callback_data="settings")],
        [InlineKeyboardButton("🧮 Exemple", callback_data="example")],
    ])


def build_analysis(*, purchase_price, resale_price, repair_cost, shipping_in, other_costs,
                    selling_fee_percent, risk_percent, settings) -> dict:
    selling_fees = resale_price * selling_fee_percent / Decimal("100")
    base_before_risk = purchase_price + repair_cost + shipping_in + other_costs + selling_fees
    risk_cost = base_before_risk * risk_percent / Decimal("100")
    total_cost = base_before_risk + risk_cost
    profit = resale_price - total_cost

    roi = profit / total_cost * Decimal("100") if total_cost > 0 else Decimal("0")
    purchase_roi = profit / purchase_price * Decimal("100") if purchase_price > 0 else Decimal("0")
    break_even_resale = total_cost

    target = settings["min_profit_eur"]
    min_roi = settings["min_roi"]

    fixed_costs = repair_cost + shipping_in + other_costs + selling_fees
    maximum_purchase_for_target = resale_price - fixed_costs - risk_cost - target

    roi_multiplier = Decimal("1") + min_roi / Decimal("100")
    maximum_purchase_for_roi = (resale_price / roi_multiplier - fixed_costs) if roi_multiplier > 0 else Decimal("0")

    recommended_max_purchase = max(Decimal("0"), min(maximum_purchase_for_target, maximum_purchase_for_roi))

    safe_resale = resale_price * settings["safety_resale_percent"] / Decimal("100")
    safe_selling_fees = safe_resale * selling_fee_percent / Decimal("100")
    safe_base_cost = purchase_price + repair_cost + shipping_in + other_costs + safe_selling_fees
    safe_risk = safe_base_cost * risk_percent / Decimal("100")
    safe_total_cost = safe_base_cost + safe_risk
    safe_profit = safe_resale - safe_total_cost

    score = calculate_score(profit, roi, purchase_roi, target, risk_cost, total_cost)
    verdict = verdict_for(profit, roi, safe_profit, target, min_roi)

    return {
        "selling_fees": selling_fees, "risk_cost": risk_cost, "total_cost": total_cost,
        "profit": profit, "roi": roi, "purchase_roi": purchase_roi,
        "break_even_resale": break_even_resale, "target_profit": target,
        "maximum_purchase": recommended_max_purchase, "safe_resale": safe_resale,
        "safe_profit": safe_profit, "score": score, "verdict": verdict,
    }


# ---------------------------------------------------------
# COMMANDES CALCULATEUR (toujours utilisables en tapant la commande,
# retirées du menu à boutons)
# ---------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    text = (
        "🤖 <b>Bot Vinted iPhone</b>\n\n"
        "<b>Calculateur :</b> /analyse, /simple, /enchere, /compare, /reglages\n"
        "<b>Menu :</b> /menu\n\n"
        "🔎 Le scan automatique Vinted tourne en tâche de fond et envoie "
        "toutes les annonces de téléphones complets dès qu'elles sont "
        "détectées — jamais d'étuis, pièces, kits ou services."
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def analyse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    if len(context.args) != 7:
        await update.effective_message.reply_text(
            "❌ <b>Il faut 7 valeurs.</b>\n\n"
            "<code>/analyse achat revente réparation livraison autres frais% risque%</code>\n\n"
            "Exemple :\n<code>/analyse 25 90 0 3.5 2 0 5</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        purchase = parse_decimal(context.args[0])
        resale = parse_decimal(context.args[1])
        repair = parse_decimal(context.args[2])
        shipping = parse_decimal(context.args[3])
        other = parse_decimal(context.args[4])
        fee_percent = parse_decimal(context.args[5])
        risk_percent = parse_decimal(context.args[6])
        if fee_percent > 100 or risk_percent > 100:
            raise ValueError("Pourcentage invalide.")
        if resale <= 0:
            raise ValueError("La revente doit être supérieure à zéro.")
    except ValueError:
        await update.effective_message.reply_text(
            "❌ Valeurs incorrectes.\n\nExemple :\n<code>/analyse 25 90 0 3.5 2 0 5</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    settings = get_settings(context)
    result = build_analysis(
        purchase_price=purchase, resale_price=resale, repair_cost=repair,
        shipping_in=shipping, other_costs=other, selling_fee_percent=fee_percent,
        risk_percent=risk_percent, settings=settings,
    )
    score = result["score"]
    score_icon = "🟢" if score >= 80 else "🟡" if score >= 60 else "🟠" if score >= 40 else "🔴"

    text = (
        f"<b>{result['verdict']}</b>\n\n"
        f"{score_icon} <b>Score : {score}/100</b>\n\n"
        "<b>📥 Achat et préparation</b>\n"
        f"Prix d'achat : {money(purchase)}\n"
        f"Réparation : {money(repair)}\n"
        f"Livraison reçue : {money(shipping)}\n"
        f"Autres coûts : {money(other)}\n"
        f"Risque prévu : {money(result['risk_cost'])}\n\n"
        "<b>📤 Revente</b>\n"
        f"Prix estimé : {money(resale)}\n"
        f"Frais de vente : {money(result['selling_fees'])} ({percent(fee_percent)})\n\n"
        "<b>📊 Résultats</b>\n"
        f"Coût total : {money(result['total_cost'])}\n"
        f"Marge nette : <b>{money(result['profit'])}</b>\n"
        f"ROI global : <b>{percent(result['roi'])}</b>\n"
        f"Profit / achat : {percent(result['purchase_roi'])}\n"
        f"Seuil de rentabilité : {money(result['break_even_resale'])}\n\n"
        "<b>🛡 Scénario prudent</b>\n"
        f"Revente réduite à {percent(settings['safety_resale_percent'])} : {money(result['safe_resale'])}\n"
        f"Marge prudente : <b>{money(result['safe_profit'])}</b>\n\n"
        "<b>🎯 Décision</b>\n"
        f"Marge minimale : {money(result['target_profit'])}\n"
        f"Prix d'achat maximal conseillé : <b>{money(result['maximum_purchase'])}</b>"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=analysis_keyboard())


async def simple(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    if len(context.args) != 4:
        await update.effective_message.reply_text(
            "Utilisation :\n<code>/simple achat revente réparation livraison</code>\n\n"
            "Exemple :\n<code>/simple 25 90 0 3.5</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        purchase = parse_decimal(context.args[0])
        resale = parse_decimal(context.args[1])
        repair = parse_decimal(context.args[2])
        shipping = parse_decimal(context.args[3])
    except ValueError:
        await update.effective_message.reply_text("❌ Format incorrect.")
        return
    settings = get_settings(context)
    context.args = [
        str(purchase), str(resale), str(repair), str(shipping), "0",
        str(settings["default_fee_percent"]), str(settings["default_risk_percent"]),
    ]
    await analyse(update, context)


async def enchere(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    if len(context.args) != 6:
        await update.effective_message.reply_text(
            "Utilisation :\n<code>/enchere revente réparation livraison autres frais% risque%</code>\n\n"
            "Exemple :\n<code>/enchere 130 0 4.5 2 0 5</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        resale = parse_decimal(context.args[0])
        repair = parse_decimal(context.args[1])
        shipping = parse_decimal(context.args[2])
        other = parse_decimal(context.args[3])
        fee_percent = parse_decimal(context.args[4])
        risk_percent = parse_decimal(context.args[5])
    except ValueError:
        await update.effective_message.reply_text("❌ Format incorrect.")
        return
    settings = get_settings(context)
    target = settings["min_profit_eur"]
    selling_fees = resale * fee_percent / Decimal("100")
    fixed_costs = repair + shipping + other + selling_fees
    preliminary_max = resale - fixed_costs - target
    risk_reserve = max(Decimal("0"), preliminary_max + fixed_costs) * risk_percent / Decimal("100")
    max_bid = max(Decimal("0"), preliminary_max - risk_reserve)
    cautious_bid = max_bid * Decimal("0.95")
    await update.effective_message.reply_text(
        "🔨 <b>Calcul d'enchère maximale</b>\n\n"
        f"Revente estimée : {money(resale)}\n"
        f"Réparation : {money(repair)}\n"
        f"Livraison : {money(shipping)}\n"
        f"Autres coûts : {money(other)}\n"
        f"Frais de vente estimés : {money(selling_fees)}\n"
        f"Réserve de risque : {money(risk_reserve)}\n"
        f"Marge visée : {money(target)}\n\n"
        f"🛑 <b>Maximum absolu : {money(max_bid)}</b>\n"
        f"✅ Enchère prudente conseillée : <b>{money(cautious_bid)}</b>",
        parse_mode=ParseMode.HTML,
    )


async def compare(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    if len(context.args) != 6:
        await update.effective_message.reply_text(
            "Compare deux affaires simplifiées :\n\n"
            "<code>/compare achat1 revente1 achat2 revente2 frais% risque%</code>\n\n"
            "Exemple :\n<code>/compare 25 90 60 150 0 5</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        buy_1 = parse_decimal(context.args[0])
        sell_1 = parse_decimal(context.args[1])
        buy_2 = parse_decimal(context.args[2])
        sell_2 = parse_decimal(context.args[3])
        fee_percent = parse_decimal(context.args[4])
        risk_percent = parse_decimal(context.args[5])
    except ValueError:
        await update.effective_message.reply_text("❌ Format incorrect.")
        return
    settings = get_settings(context)
    first = build_analysis(purchase_price=buy_1, resale_price=sell_1, repair_cost=Decimal("0"),
                            shipping_in=Decimal("0"), other_costs=Decimal("0"),
                            selling_fee_percent=fee_percent, risk_percent=risk_percent,
                            settings=settings)
    second = build_analysis(purchase_price=buy_2, resale_price=sell_2, repair_cost=Decimal("0"),
                             shipping_in=Decimal("0"), other_costs=Decimal("0"),
                             selling_fee_percent=fee_percent, risk_percent=risk_percent,
                             settings=settings)
    winner = "Affaire 1" if first["score"] >= second["score"] else "Affaire 2"
    await update.effective_message.reply_text(
        "⚖️ <b>Comparaison</b>\n\n"
        f"<b>Affaire 1</b>\nMarge : {money(first['profit'])}\nROI : {percent(first['roi'])}\nScore : {first['score']}/100\n\n"
        f"<b>Affaire 2</b>\nMarge : {money(second['profit'])}\nROI : {percent(second['roi'])}\nScore : {second['score']}/100\n\n"
        f"🏆 Meilleur choix : <b>{winner}</b>",
        parse_mode=ParseMode.HTML,
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    settings = get_settings(context)
    await update.effective_message.reply_text(
        "⚙️ <b>Tes réglages</b>\n\n"
        f"Marge minimale : {money(settings['min_profit_eur'])}\n"
        f"ROI minimum : {percent(settings['min_roi'])}\n"
        f"Frais de vente par défaut : {percent(settings['default_fee_percent'])} "
        "(0% par défaut — Vinted ne facture rien au vendeur)\n"
        f"Réserve de risque par défaut : {percent(settings['default_risk_percent'])}\n"
        f"Scénario prudent : {percent(settings['safety_resale_percent'])} du prix de revente\n\n"
        f"Catégorie de recherche active : {get_current_category_label()}\n"
        f"Fréquence de scan : toutes les {CYCLE_SECONDS} s\n\n"
        "<b>Modifier :</b>\n<code>/setmarge 40</code>\n<code>/setroi 25</code>",
        parse_mode=ParseMode.HTML,
    )


async def set_margin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    if len(context.args) != 1:
        await update.effective_message.reply_text("Exemple : <code>/setmarge 40</code>", parse_mode=ParseMode.HTML)
        return
    try:
        value = parse_decimal(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Entre un montant positif.")
        return
    settings = get_settings(context)
    settings["min_profit_eur"] = value
    await update.effective_message.reply_text(f"✅ Marge minimale réglée à {money(value)}.")


async def set_roi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    if len(context.args) != 1:
        await update.effective_message.reply_text("Exemple : <code>/setroi 25</code>", parse_mode=ParseMode.HTML)
        return
    try:
        value = parse_decimal(context.args[0])
        if value > 500:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("❌ Entre un pourcentage valide.")
        return
    settings = get_settings(context)
    settings["min_roi"] = value
    await update.effective_message.reply_text(f"✅ ROI minimum réglé à {percent(value)}.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    await update.effective_message.reply_text(
        "📘 <b>Guide</b>\n\n"
        "<b>Analyse complète</b>\n<code>/analyse achat revente réparation livraison autres frais% risque%</code>\n\n"
        "<b>Analyse simplifiée</b>\n<code>/simple achat revente réparation livraison</code>\n\n"
        "<b>Enchère maximale</b>\n<code>/enchere revente réparation livraison autres frais% risque%</code>\n\n"
        "<b>Comparer</b>\n<code>/compare achat1 revente1 achat2 revente2 frais% risque%</code>\n\n"
        "<b>Réglages</b>\n<code>/reglages</code> · <code>/setmarge 40</code> · <code>/setroi 25</code>\n\n"
        "<b>Scan</b>\n<code>/scan</code> lance un scan manuel immédiat\n\n"
        "<b>Menu :</b> /menu",
        parse_mode=ParseMode.HTML,
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if query.data == "help_quick":
        await query.message.reply_text("<code>/analyse 25 90 0 3.5 2 0 5</code>", parse_mode=ParseMode.HTML)
    elif query.data == "settings":
        await settings_command(update, context)
    elif query.data == "example":
        await query.message.reply_text("<code>/analyse 25 90 0 3.5 2 0 5</code>", parse_mode=ParseMode.HTML)
    elif query.data == "menu_scan":
        await query.message.reply_text("🔎 Scan manuel lancé, un instant...")
        await run_scan_cycle(context, manual=True, reply_message=query.message)
    elif query.data == "menu_state":
        await send_bot_state(query.message, context)
    elif query.data == "menu_filters":
        await send_filters_summary(query.message)
    elif query.data == "menu_history":
        await send_history(query.message)
    elif query.data == "cat_broken":
        set_category("broken")
        await query.message.reply_text(
            "📱 Catégorie réglée sur : <b>iPhone cassé / bloqué / pour pièces</b>\n"
            "Scan lancé sur cette catégorie...",
            parse_mode=ParseMode.HTML,
        )
        await run_scan_cycle(context, manual=True, reply_message=query.message)
    elif query.data == "cat_all":
        set_category("all")
        await query.message.reply_text(
            "📦 Catégorie réglée sur : <b>Tous les iPhone</b>\n"
            "Scan lancé sur cette catégorie...",
            parse_mode=ParseMode.HTML,
        )
        await run_scan_cycle(context, manual=True, reply_message=query.message)
    elif query.data.startswith("fb:"):
        await handle_listing_feedback(query, context)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Scanner maintenant", callback_data="menu_scan"),
         InlineKeyboardButton("📊 État du bot", callback_data="menu_state")],
        [InlineKeyboardButton("📱 Cassé/bloqué/pièces", callback_data="cat_broken"),
         InlineKeyboardButton("📦 Tous les iPhone", callback_data="cat_all")],
        [InlineKeyboardButton("🧰 Filtres", callback_data="menu_filters"),
         InlineKeyboardButton("🕘 Historique", callback_data="menu_history")],
    ])


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    await update.effective_message.reply_text(
        f"📋 <b>Menu</b>\nCatégorie active : <b>{get_current_category_label()}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )


async def send_bot_state(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    anthropic_status = "désactivé (crédit insuffisant)" if normalize_module.ANTHROPIC_DISABLED else (
        "actif" if USE_ANTHROPIC_NORMALIZER else "désactivé (config)"
    )
    await message.reply_text(
        "📊 <b>État du bot</b>\n\n"
        f"Catégorie active : {get_current_category_label()}\n"
        f"Dernier scan — trouvées : {BOT_STATE['last_found']}\n"
        f"Dernier scan — envoyées : {BOT_STATE['last_sent']}\n"
        f"Dernier scan — doublons : {BOT_STATE['last_duplicates']}\n"
        f"Dernière erreur : {BOT_STATE['last_error'] or 'aucune'}\n"
        f"Normaliseur Anthropic : {anthropic_status}",
        parse_mode=ParseMode.HTML,
    )


async def send_filters_summary(message) -> None:
    from pipeline.relevance import (
        BUYER_PATTERNS, SERVICE_PATTERNS, ACCESSORY_PATTERNS,
        PARTS_PATTERNS, CATALOG_PATTERNS, OTHER_BRAND_PATTERNS,
    )
    await message.reply_text(
        "🧰 <b>Filtres actifs</b>\n\n"
        f"Acheteurs : {len(BUYER_PATTERNS)} règles\n"
        f"Services/réparateurs : {len(SERVICE_PATTERNS)} règles\n"
        f"Accessoires : {len(ACCESSORY_PATTERNS)} règles\n"
        f"Pièces/kits : {len(PARTS_PATTERNS)} règles\n"
        f"Catalogues : {len(CATALOG_PATTERNS)} règles\n"
        f"Autres marques : {len(OTHER_BRAND_PATTERNS)} règles\n\n"
        f"Catégorie de recherche active : {get_current_category_label()}\n"
        "Seuls les téléphones complets (whole_phone) sont envoyés — aucun filtre de score.",
        parse_mode=ParseMode.HTML,
    )


async def send_history(message) -> None:
    try:
        rows = get_recent_sent(10)
    except Exception:
        LOGGER.exception("Échec récupération historique")
        await message.reply_text("❌ Impossible de récupérer l'historique pour le moment.")
        return
    if not rows:
        await message.reply_text("🕘 Aucune annonce envoyée pour le moment.")
        return
    lines = ["🕘 <b>Dernières annonces envoyées</b>\n"]
    for row in rows:
        lines.append(f"• {row.get('model') or '?'} — {row.get('estimated_margin_eur')} €")
    await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def handle_listing_feedback(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        _, listing_id, feedback = query.data.split(":", 2)
    except ValueError:
        return
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE listings SET feedback = %s WHERE id = %s",
                    (feedback, listing_id),
                )
                conn.commit()
    except Exception:
        LOGGER.exception("Échec enregistrement feedback pour %s", listing_id)
        return
    labels = {"interesting": "👍 Intéressant", "not_interesting": "👎 Pas intéressant", "bad_listing": "🚫 Mauvaise annonce"}
    await query.message.reply_text(f"Merci, noté : {labels.get(feedback, feedback)}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.exception("Erreur pendant le traitement d'une mise à jour", exc_info=context.error)
    BOT_STATE["last_error"] = str(context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Une erreur est survenue. Réessaie avec /aide.")
        except Exception:
            LOGGER.exception("Impossible d'envoyer le message d'erreur.")


# ---------------------------------------------------------
# NOTIFICATION D'UNE ANNONCE (format façon Discord, sans score)
# ---------------------------------------------------------

def _format_listing_age(posted_at_iso: str | None) -> str:
    if not posted_at_iso:
        return "date de publication inconnue"
    try:
        posted_dt = datetime.fromisoformat(posted_at_iso)
    except ValueError:
        return "date de publication inconnue"
    age = datetime.now(timezone.utc) - posted_dt
    hours = age.total_seconds() / 3600
    if hours < 1:
        return f"il y a {int(age.total_seconds() / 60)} min"
    if hours < 24:
        return f"il y a {hours:.1f} h"
    return f"il y a {hours / 24:.1f} j"


_CONDITION_LABELS = {
    "icloud_locked": "iCloud verrouillé",
    "water_damage": "Dégâts d'eau",
    "no_power": "Ne s'allume plus",
    "cracked_screen": "Écran fissuré",
    "battery_issue": "Batterie à changer",
    "charging_issue": "Port de charge défectueux",
    "carrier_locked": "Bloqué opérateur",
    "for_parts": "Pour pièces",
    "functional": "Fonctionnel",
    "unknown": "État non précisé",
}


def _format_deal_message(listing: dict) -> str:
    n = listing.get("normalized", {})
    e = listing.get("estimation", {})
    price = e.get("listing_price_eur")
    condition_label = _CONDITION_LABELS.get(n.get("condition", "unknown"), "État non précisé")

    lines = [
        f"📱 <b>{n.get('model') or listing.get('title', '?')}</b>\n",
        f"⏳ <b>Publié</b>\n{_format_listing_age(listing.get('posted_at'))}\n",
        f"🏷️ <b>Marque</b>\nApple\n",
        f"💎 <b>État</b>\n{condition_label}\n",
        f"💰 <b>Prix</b>\n{price} €" if price is not None else "💰 <b>Prix</b>\nnon précisé",
    ]
    return "\n".join(lines)


def _listing_keyboard(listing_id: str, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Ouvrir l'annonce", url=url)],
        [
            InlineKeyboardButton("👍 Intéressant", callback_data=f"fb:{listing_id}:interesting"),
            InlineKeyboardButton("👎 Pas intéressant", callback_data=f"fb:{listing_id}:not_interesting"),
            InlineKeyboardButton("🚫 Mauvaise annonce", callback_data=f"fb:{listing_id}:bad_listing"),
        ],
    ])


# ---------------------------------------------------------
# CYCLE DE SCAN — envoie toutes les annonces whole_phone, sans score
# ---------------------------------------------------------

async def run_scan_cycle(context: ContextTypes.DEFAULT_TYPE, manual: bool = False, reply_message=None) -> dict:
    if SCAN_LOCK.locked():
        if reply_message:
            await reply_message.reply_text("⏳ Un scan est déjà en cours, réessaie dans un instant.")
        return {}

    async with SCAN_LOCK:
        counters = {
            "found_raw": 0, "accepted_source": 0,
            "rejected_accessory": 0, "rejected_parts": 0, "rejected_service": 0,
            "rejected_buyer": 0, "rejected_other_brand": 0, "rejected_catalog": 0,
            "rejected_ambiguous": 0, "rejected_too_old": 0, "rejected_too_far": 0,
            "duplicates": 0, "sent": 0, "errors": 0,
        }

        for fetch_source in SOURCES:
            try:
                raw_listings = await asyncio.get_event_loop().run_in_executor(None, fetch_source)
            except Exception:
                LOGGER.exception("Échec de la source %s", getattr(fetch_source, "__module__", fetch_source))
                counters["errors"] += 1
                continue

            try:
                stats_getter = SOURCE_STATS_GETTERS.get(fetch_source)
                source_stats = stats_getter() if stats_getter else {}
                counters["found_raw"] += source_stats.get("found_raw", 0)
                counters["accepted_source"] += source_stats.get("accepted_source", 0)
                counters["rejected_too_old"] += source_stats.get("rejected_too_old", 0)
                counters["rejected_too_far"] += source_stats.get("rejected_too_far", 0)
                counters["errors"] += source_stats.get("errors", 0)
            except Exception:
                pass

            for listing in raw_listings:
                try:
                    listing = normalize_listing(listing)
                    relevance = listing.get("relevance", {})
                    listing_type = relevance.get("listing_type")

                    if not relevance.get("is_relevant", False):
                        bucket = {
                            "accessory": "rejected_accessory",
                            "part_only": "rejected_parts",
                            "repair_service": "rejected_service",
                            "buyer_ad": "rejected_buyer",
                            "other_brand": "rejected_other_brand",
                            "shop_catalog": "rejected_catalog",
                            "ambiguous": "rejected_ambiguous",
                        }.get(listing_type)
                        if bucket:
                            counters[bucket] += 1
                        continue

                    listing = estimate_listing(listing)

                    listing_id = insert_listing(listing)
                    if listing_id is None:
                        counters["duplicates"] += 1
                        continue

                    if listing.get("relevance", {}).get("listing_type") != "whole_phone":
                        continue

                    if ALLOWED_CHAT_ID:
                        photos = listing.get("photos") or []
                        caption = _format_deal_message(listing)
                        keyboard = _listing_keyboard(str(listing_id), listing["url"])
                        if photos:
                            await context.bot.send_photo(
                                chat_id=ALLOWED_CHAT_ID,
                                photo=photos[0],
                                caption=caption,
                                parse_mode=ParseMode.HTML,
                                reply_markup=keyboard,
                            )
                        else:
                            await context.bot.send_message(
                                chat_id=ALLOWED_CHAT_ID,
                                text=caption,
                                parse_mode=ParseMode.HTML,
                                reply_markup=keyboard,
                            )
                        counters["sent"] += 1

                except Exception:
                    LOGGER.exception("Échec pipeline pour l'annonce: %s", listing.get("title"))
                    counters["errors"] += 1

        BOT_STATE["last_found"] = counters["found_raw"]
        BOT_STATE["last_sent"] = counters["sent"]
        BOT_STATE["last_duplicates"] = counters["duplicates"]
        BOT_STATE["last_error"] = None if counters["errors"] == 0 else f"{counters['errors']} erreur(s)"

        LOGGER.info("Statistiques du scan: %s", counters)

        if manual and reply_message and ALLOWED_CHAT_ID:
            await context.bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text=(
                    "✅ <b>Scan terminé</b>\n\n"
                    f"Trouvées : {counters['found_raw']} · Envoyées : {counters['sent']} · "
                    f"Doublons : {counters['duplicates']}\n"
                    f"Rejetées — accessoires : {counters['rejected_accessory']}, "
                    f"pièces : {counters['rejected_parts']}, services : {counters['rejected_service']}, "
                    f"acheteurs : {counters['rejected_buyer']}, autres marques : {counters['rejected_other_brand']}, "
                    f"catalogues : {counters['rejected_catalog']}, ambiguës : {counters['rejected_ambiguous']}\n"
                    f"Trop vieilles : {counters['rejected_too_old']} · Trop loin : {counters['rejected_too_far']}"
                ),
                parse_mode=ParseMode.HTML,
            )

        return counters


async def scan_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_scan_cycle(context, manual=False)


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    await update.effective_message.reply_text("🔎 Scan manuel lancé, un instant...")
    await run_scan_cycle(context, manual=True, reply_message=update.effective_message)


# ---------------------------------------------------------
# LANCEMENT
# ---------------------------------------------------------

async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([
        ("start", "Démarrer le bot"),
        ("menu", "Menu principal"),
        ("scan", "Lancer un scan manuel"),
        ("analyse", "Analyse complète"),
        ("simple", "Analyse simplifiée"),
        ("enchere", "Calculer une enchère maximale"),
        ("compare", "Comparer deux affaires"),
        ("reglages", "Afficher les réglages"),
        ("setmarge", "Modifier la marge minimale"),
        ("setroi", "Modifier le ROI minimum"),
        ("aide", "Afficher le guide"),
    ])
    LOGGER.info("Commandes Telegram configurées.")


def main() -> None:
    if not TOKEN:
        raise RuntimeError("La variable TELEGRAM_BOT_TOKEN est absente.")
    if not ALLOWED_CHAT_ID:
        LOGGER.warning("TELEGRAM_CHAT_ID absent : le scan automatique ne pourra pas notifier.")

    persistence = PicklePersistence(filepath=PERSISTENCE_FILE)
    application = (
        Application.builder().token(TOKEN).persistence(persistence).post_init(post_init).build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("scan", scan_command))
    application.add_handler(CommandHandler("analyse", analyse))
    application.add_handler(CommandHandler("simple", simple))
    application.add_handler(CommandHandler("rapide", simple))
    application.add_handler(CommandHandler("enchere", enchere))
    application.add_handler(CommandHandler("compare", compare))
    application.add_handler(CommandHandler("reglages", settings_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("setmarge", set_margin))
    application.add_handler(CommandHandler("setroi", set_roi))
    application.add_handler(CommandHandler("aide", help_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(button_handler))

    application.add_error_handler(error_handler)

    application.job_queue.run_repeating(scan_job, interval=CYCLE_SECONDS, first=15)

    LOGGER.info("Bot Vinted démarré. Scan auto toutes les %ds.", CYCLE_SECONDS)

    application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
