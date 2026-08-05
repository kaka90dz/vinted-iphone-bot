import sys
import io
import logging
import os
import asyncio
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Final
from datetime import datetime, timezone

# Corrige l'encodage corrompu observé dans les logs ("modÃ¨le", "Ã©tat") :
# force stdout/stderr en UTF-8 explicite, avant toute autre initialisation.
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

from sources.facebook_marketplace import (
    fetch as fetch_facebook,
    get_last_stats as get_facebook_stats,
    MAX_DISTANCE_KM,
    FACEBOOK_MARKETPLACE_CITY,
    ACCEPT_UNKNOWN_LOCATION,
    MAX_LISTING_AGE_HOURS,
    ACCEPT_UNKNOWN_DATE,
)
from sources.vinted import (
    fetch as fetch_vinted,
    get_last_stats as get_vinted_stats,
)
from pipeline.normalize import normalize_listing, USE_ANTHROPIC_NORMALIZER
import pipeline.normalize as normalize_module
from pipeline.estimate import estimate_listing
from pipeline.score import score_listing
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

PERSISTENCE_FILE: Final[str] = os.getenv("PERSISTENCE_FILE", "/tmp/iphone_bot_data.pickle")

SUPPORTED_CURRENCIES: Final[set[str]] = {"EUR", "CAD"}

DEFAULT_SETTINGS: Final[dict[str, Decimal]] = {
    "min_profit_eur": Decimal("40"),
    "min_profit_cad": Decimal("60"),
    "min_roi": Decimal("20"),
    "default_fee_percent": Decimal("13"),
    "default_risk_percent": Decimal("5"),
    "safety_resale_percent": Decimal("90"),
}

MONEY_STEP: Final[Decimal] = Decimal("0.01")
PERCENT_STEP: Final[Decimal] = Decimal("0.1")

CYCLE_SECONDS: Final[int] = int(os.getenv("CYCLE_SECONDS", "900"))
DEFAULT_SCORE_THRESHOLD: Final[int] = int(os.getenv("SCORE_THRESHOLD", "55"))
SOURCES = [fetch_facebook, fetch_vinted]

# Chaque source expose ses propres stats (found_raw, accepted_source, etc.)
# via sa fonction get_last_stats() ; cette table permet à run_scan_cycle
# d'appeler la bonne selon la source en cours, plutôt que de supposer
# Facebook Marketplace comme seule source possible.
SOURCE_STATS_GETTERS = {
    fetch_facebook: get_facebook_stats,
    fetch_vinted: get_vinted_stats,
}

# Empêche un scan manuel ("Scanner maintenant") et le scan automatique
# planifié de se chevaucher.
SCAN_LOCK = asyncio.Lock()

# État du bot exposé par le bouton "État du bot".
BOT_STATE = {
    "last_found": 0,
    "last_sent": 0,
    "last_duplicates": 0,
    "last_error": None,
}


# ---------------------------------------------------------
# OUTILS CALCULATEUR (inchangés)
# ---------------------------------------------------------

def parse_decimal(value: str) -> Decimal:
    cleaned = (
        value.strip().replace("€", "").replace("$", "").replace("%", "")
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


def money(value: Decimal, currency: str) -> str:
    rounded = value.quantize(MONEY_STEP, rounding=ROUND_HALF_UP)
    return f"{rounded} $ CA" if currency == "CAD" else f"{rounded} €"


def percent(value: Decimal) -> str:
    return f"{value.quantize(PERCENT_STEP, rounding=ROUND_HALF_UP)} %"


def get_settings(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Decimal]:
    settings = context.user_data.setdefault("settings", DEFAULT_SETTINGS.copy())
    for key, value in DEFAULT_SETTINGS.items():
        settings.setdefault(key, value)
    return settings


def target_profit(currency: str, settings: dict[str, Decimal]) -> Decimal:
    return settings["min_profit_cad"] if currency == "CAD" else settings["min_profit_eur"]


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
        [InlineKeyboardButton("📘 Aide", callback_data="help"),
         InlineKeyboardButton("⚙️ Réglages", callback_data="settings")],
        [InlineKeyboardButton("🧮 Exemple France", callback_data="example_eur"),
         InlineKeyboardButton("🇨🇦 Exemple Canada", callback_data="example_cad")],
    ])


def build_analysis(*, purchase_price, resale_price, repair_cost, shipping_in, other_costs,
                    selling_fee_percent, risk_percent, currency, settings) -> dict:
    selling_fees = resale_price * selling_fee_percent / Decimal("100")
    base_before_risk = purchase_price + repair_cost + shipping_in + other_costs + selling_fees
    risk_cost = base_before_risk * risk_percent / Decimal("100")
    total_cost = base_before_risk + risk_cost
    profit = resale_price - total_cost

    roi = profit / total_cost * Decimal("100") if total_cost > 0 else Decimal("0")
    purchase_roi = profit / purchase_price * Decimal("100") if purchase_price > 0 else Decimal("0")
    break_even_resale = total_cost

    target = target_profit(currency, settings)
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
# COMMANDES CALCULATEUR (inchangées, condensées)
# ---------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    text = (
        "🤖 <b>Bot iPhone Marketplace</b>\n\n"
        "<b>Calculateur :</b> /analyse, /simple, /enchere, /compare, /reglages\n"
        "<b>Menu complet :</b> /menu\n\n"
        "🔎 Le scan automatique tourne en tâche de fond "
        "et n'envoie que des annonces de téléphones complets — jamais "
        "d'étuis, pièces, kits ou services."
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=analysis_keyboard())


async def analyse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    if len(context.args) != 8:
        await update.effective_message.reply_text(
            "❌ <b>Il faut 8 valeurs.</b>\n\n"
            "<code>/analyse achat revente réparation livraison autres frais% risque% devise</code>\n\n"
            "Exemple :\n<code>/analyse 38 120 0 4.15 3 13 5 EUR</code>",
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
        currency = context.args[7].upper()
        if currency not in SUPPORTED_CURRENCIES:
            raise ValueError("Devise invalide.")
        if fee_percent > 100 or risk_percent > 100:
            raise ValueError("Pourcentage invalide.")
        if resale <= 0:
            raise ValueError("La revente doit être supérieure à zéro.")
    except ValueError:
        await update.effective_message.reply_text(
            "❌ Valeurs incorrectes.\n\nExemple :\n<code>/analyse 38 120 0 4.15 3 13 5 EUR</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    settings = get_settings(context)
    result = build_analysis(
        purchase_price=purchase, resale_price=resale, repair_cost=repair,
        shipping_in=shipping, other_costs=other, selling_fee_percent=fee_percent,
        risk_percent=risk_percent, currency=currency, settings=settings,
    )
    score = result["score"]
    score_icon = "🟢" if score >= 80 else "🟡" if score >= 60 else "🟠" if score >= 40 else "🔴"

    text = (
        f"<b>{result['verdict']}</b>\n\n"
        f"{score_icon} <b>Score : {score}/100</b>\n\n"
        "<b>📥 Achat et préparation</b>\n"
        f"Prix d'achat : {money(purchase, currency)}\n"
        f"Réparation : {money(repair, currency)}\n"
        f"Livraison reçue : {money(shipping, currency)}\n"
        f"Autres coûts : {money(other, currency)}\n"
        f"Risque prévu : {money(result['risk_cost'], currency)}\n\n"
        "<b>📤 Revente</b>\n"
        f"Prix estimé : {money(resale, currency)}\n"
        f"Frais de vente : {money(result['selling_fees'], currency)} ({percent(fee_percent)})\n\n"
        "<b>📊 Résultats</b>\n"
        f"Coût total : {money(result['total_cost'], currency)}\n"
        f"Marge nette : <b>{money(result['profit'], currency)}</b>\n"
        f"ROI global : <b>{percent(result['roi'])}</b>\n"
        f"Profit / achat : {percent(result['purchase_roi'])}\n"
        f"Seuil de rentabilité : {money(result['break_even_resale'], currency)}\n\n"
        "<b>🛡 Scénario prudent</b>\n"
        f"Revente réduite à {percent(settings['safety_resale_percent'])} : {money(result['safe_resale'], currency)}\n"
        f"Marge prudente : <b>{money(result['safe_profit'], currency)}</b>\n\n"
        "<b>🎯 Décision</b>\n"
        f"Marge minimale : {money(result['target_profit'], currency)}\n"
        f"Prix d'achat maximal conseillé : <b>{money(result['maximum_purchase'], currency)}</b>"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=analysis_keyboard())


async def simple(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    if len(context.args) != 5:
        await update.effective_message.reply_text(
            "Utilisation :\n<code>/simple achat revente réparation livraison devise</code>\n\n"
            "Exemple :\n<code>/simple 38 120 0 4.15 EUR</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        purchase = parse_decimal(context.args[0])
        resale = parse_decimal(context.args[1])
        repair = parse_decimal(context.args[2])
        shipping = parse_decimal(context.args[3])
        currency = context.args[4].upper()
        if currency not in SUPPORTED_CURRENCIES:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("❌ Format incorrect.")
        return
    settings = get_settings(context)
    context.args = [
        str(purchase), str(resale), str(repair), str(shipping), "0",
        str(settings["default_fee_percent"]), str(settings["default_risk_percent"]), currency,
    ]
    await analyse(update, context)


async def enchere(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    if len(context.args) != 7:
        await update.effective_message.reply_text(
            "Utilisation :\n<code>/enchere revente réparation livraison autres frais% risque% devise</code>\n\n"
            "Exemple :\n<code>/enchere 260 0 5.99 3 13 5 EUR</code>",
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
        currency = context.args[6].upper()
        if currency not in SUPPORTED_CURRENCIES:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("❌ Format incorrect.")
        return
    settings = get_settings(context)
    target = target_profit(currency, settings)
    selling_fees = resale * fee_percent / Decimal("100")
    fixed_costs = repair + shipping + other + selling_fees
    preliminary_max = resale - fixed_costs - target
    risk_reserve = max(Decimal("0"), preliminary_max + fixed_costs) * risk_percent / Decimal("100")
    max_bid = max(Decimal("0"), preliminary_max - risk_reserve)
    cautious_bid = max_bid * Decimal("0.95")
    await update.effective_message.reply_text(
        "🔨 <b>Calcul d'enchère maximale</b>\n\n"
        f"Revente estimée : {money(resale, currency)}\n"
        f"Réparation : {money(repair, currency)}\n"
        f"Livraison : {money(shipping, currency)}\n"
        f"Autres coûts : {money(other, currency)}\n"
        f"Frais de vente estimés : {money(selling_fees, currency)}\n"
        f"Réserve de risque : {money(risk_reserve, currency)}\n"
        f"Marge visée : {money(target, currency)}\n\n"
        f"🛑 <b>Maximum absolu : {money(max_bid, currency)}</b>\n"
        f"✅ Enchère prudente conseillée : <b>{money(cautious_bid, currency)}</b>",
        parse_mode=ParseMode.HTML,
    )


async def compare(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    if len(context.args) != 7:
        await update.effective_message.reply_text(
            "Compare deux affaires simplifiées :\n\n"
            "<code>/compare achat1 revente1 achat2 revente2 frais% risque% devise</code>\n\n"
            "Exemple :\n<code>/compare 38 120 110 240 13 5 EUR</code>",
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
        currency = context.args[6].upper()
        if currency not in SUPPORTED_CURRENCIES:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("❌ Format incorrect.")
        return
    settings = get_settings(context)
    first = build_analysis(purchase_price=buy_1, resale_price=sell_1, repair_cost=Decimal("0"),
                            shipping_in=Decimal("0"), other_costs=Decimal("0"),
                            selling_fee_percent=fee_percent, risk_percent=risk_percent,
                            currency=currency, settings=settings)
    second = build_analysis(purchase_price=buy_2, resale_price=sell_2, repair_cost=Decimal("0"),
                             shipping_in=Decimal("0"), other_costs=Decimal("0"),
                             selling_fee_percent=fee_percent, risk_percent=risk_percent,
                             currency=currency, settings=settings)
    winner = "Affaire 1" if first["score"] >= second["score"] else "Affaire 2"
    await update.effective_message.reply_text(
        "⚖️ <b>Comparaison</b>\n\n"
        f"<b>Affaire 1</b>\nMarge : {money(first['profit'], currency)}\nROI : {percent(first['roi'])}\nScore : {first['score']}/100\n\n"
        f"<b>Affaire 2</b>\nMarge : {money(second['profit'], currency)}\nROI : {percent(second['roi'])}\nScore : {second['score']}/100\n\n"
        f"🏆 Meilleur choix : <b>{winner}</b>",
        parse_mode=ParseMode.HTML,
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    settings = get_settings(context)
    threshold = context.bot_data.get("score_threshold", DEFAULT_SCORE_THRESHOLD)
    await update.effective_message.reply_text(
        "⚙️ <b>Tes réglages</b>\n\n"
        f"Marge minimale EUR : {money(settings['min_profit_eur'], 'EUR')}\n"
        f"Marge minimale CAD : {money(settings['min_profit_cad'], 'CAD')}\n"
        f"ROI minimum : {percent(settings['min_roi'])}\n"
        f"Frais de vente par défaut : {percent(settings['default_fee_percent'])}\n"
        f"Réserve de risque par défaut : {percent(settings['default_risk_percent'])}\n"
        f"Scénario prudent : {percent(settings['safety_resale_percent'])} du prix de revente\n\n"
        f"Seuil de score scan auto : {threshold}/100\n"
        f"Fréquence de scan : toutes les {CYCLE_SECONDS // 60} min\n\n"
        "<b>Modifier :</b>\n<code>/setmarge EUR 40</code>\n<code>/setmarge CAD 60</code>\n"
        "<code>/setroi 20</code>\n<code>/setscore 55</code>",
        parse_mode=ParseMode.HTML,
    )


async def set_margin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    if len(context.args) != 2:
        await update.effective_message.reply_text("Exemple : <code>/setmarge EUR 40</code>", parse_mode=ParseMode.HTML)
        return
    currency = context.args[0].upper()
    try:
        value = parse_decimal(context.args[1])
        if currency not in SUPPORTED_CURRENCIES:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("❌ Utilise EUR ou CAD et un montant positif.")
        return
    settings = get_settings(context)
    if currency == "EUR":
        settings["min_profit_eur"] = value
    else:
        settings["min_profit_cad"] = value
    await update.effective_message.reply_text(f"✅ Marge minimale {currency} réglée à {money(value, currency)}.")


async def set_roi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    if len(context.args) != 1:
        await update.effective_message.reply_text("Exemple : <code>/setroi 20</code>", parse_mode=ParseMode.HTML)
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


async def set_score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Modifie le seuil de score au-delà duquel le scan auto notifie."""
    if not await check_access(update):
        return
    if len(context.args) != 1:
        await update.effective_message.reply_text("Exemple : <code>/setscore 55</code>", parse_mode=ParseMode.HTML)
        return
    try:
        value = int(context.args[0])
        if not (0 <= value <= 100):
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("❌ Entre un entier entre 0 et 100.")
        return
    context.bot_data["score_threshold"] = value
    await update.effective_message.reply_text(f"✅ Seuil de score scan auto réglé à {value}/100.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    await update.effective_message.reply_text(
        "📘 <b>Guide</b>\n\n"
        "<b>Analyse complète</b>\n<code>/analyse achat revente réparation livraison autres frais% risque% devise</code>\n\n"
        "<b>Analyse simplifiée</b>\n<code>/simple achat revente réparation livraison devise</code>\n\n"
        "<b>Enchère maximale</b>\n<code>/enchere revente réparation livraison autres frais% risque% devise</code>\n\n"
        "<b>Comparer</b>\n<code>/compare achat1 revente1 achat2 revente2 frais% risque% devise</code>\n\n"
        "<b>Menu complet :</b> /menu",
        parse_mode=ParseMode.HTML,
        reply_markup=analysis_keyboard(),
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if query.data == "help":
        await query.message.reply_text("<code>/analyse 38 120 0 4.15 3 13 5 EUR</code>", parse_mode=ParseMode.HTML)
    elif query.data == "settings":
        await settings_command(update, context)
    elif query.data == "example_eur":
        await query.message.reply_text("<code>/analyse 38 120 0 4.15 3 13 5 EUR</code>", parse_mode=ParseMode.HTML)
    elif query.data == "example_cad":
        await query.message.reply_text("<code>/analyse 80 180 20 15 5 13 5 CAD</code>", parse_mode=ParseMode.HTML)
    elif query.data == "menu_scan":
        await query.message.reply_text("🔎 Scan manuel lancé, un instant...")
        await run_scan_cycle(context, manual=True, reply_message=query.message)
    elif query.data == "menu_state":
        await send_bot_state(query.message, context)
    elif query.data == "menu_filters":
        await send_filters_summary(query.message)
    elif query.data == "menu_score":
        threshold = context.bot_data.get("score_threshold", DEFAULT_SCORE_THRESHOLD)
        await query.message.reply_text(
            f"🎯 Seuil de score actuel : <b>{threshold}/100</b>\nModifier : <code>/setscore 60</code>",
            parse_mode=ParseMode.HTML,
        )
    elif query.data == "menu_frequency":
        await query.message.reply_text(
            f"⏱ Fréquence de scan : toutes les <b>{CYCLE_SECONDS // 60} min</b>\n"
            "Modifiable via la variable d'environnement <code>CYCLE_SECONDS</code> sur Railway "
            "(redéploiement nécessaire — le planificateur est fixé au démarrage).",
            parse_mode=ParseMode.HTML,
        )
    elif query.data == "menu_zone":
        await query.message.reply_text(
            f"📍 Zone Facebook Marketplace : <b>{FACEBOOK_MARKETPLACE_CITY}</b>, rayon <b>{MAX_DISTANCE_KM} km</b>\n"
            f"Localisation inconnue acceptée : {'oui' if ACCEPT_UNKNOWN_LOCATION else 'non'}\n"
            f"Ancienneté max : {MAX_LISTING_AGE_HOURS}h (date inconnue acceptée : "
            f"{'oui' if ACCEPT_UNKNOWN_DATE else 'non'})\n\n"
            "📍 Zone Vinted : France entière (pas de filtre géographique ni d'ancienneté "
            "pour cette source).",
            parse_mode=ParseMode.HTML,
        )
    elif query.data == "menu_history":
        await send_history(query.message)
    elif query.data.startswith("fb:"):
        await handle_listing_feedback(query, context)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Scanner maintenant", callback_data="menu_scan"),
         InlineKeyboardButton("📊 État du bot", callback_data="menu_state")],
        [InlineKeyboardButton("🧰 Filtres", callback_data="menu_filters"),
         InlineKeyboardButton("🎯 Score minimum", callback_data="menu_score")],
        [InlineKeyboardButton("⏱ Fréquence", callback_data="menu_frequency"),
         InlineKeyboardButton("📍 Zone", callback_data="menu_zone")],
        [InlineKeyboardButton("🕘 Historique", callback_data="menu_history")],
    ])


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    await update.effective_message.reply_text(
        "📋 <b>Menu</b>", parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard()
    )


async def send_bot_state(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    anthropic_status = "désactivé (crédit insuffisant)" if normalize_module.ANTHROPIC_DISABLED else (
        "actif" if USE_ANTHROPIC_NORMALIZER else "désactivé (config)"
    )
    await message.reply_text(
        "📊 <b>État du bot</b>\n\n"
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
        "Seuls les téléphones complets (whole_phone) passent au scoring.",
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
        lines.append(
            f"• {row.get('model') or '?'} — score {row.get('score')} — "
            f"{row.get('estimated_margin_cad')} $ CA"
        )
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


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    await update.effective_message.reply_text("Commande inconnue. Envoie /menu ou /aide.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.exception("Erreur pendant le traitement d'une mise à jour", exc_info=context.error)
    BOT_STATE["last_error"] = str(context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Une erreur est survenue. Réessaie avec /aide.")
        except Exception:
            LOGGER.exception("Impossible d'envoyer le message d'erreur.")


# ---------------------------------------------------------
# NOTIFICATION D'UNE BONNE AFFAIRE
# ---------------------------------------------------------

def _format_listing_age(posted_at_iso: str | None) -> str:
    """Traduit posted_at (ISO ou None) en texte lisible. Une date inconnue
    reste explicitement signalée comme telle — jamais devinée."""
    if not posted_at_iso:
        return "date de publication inconnue"
    try:
        posted_dt = datetime.fromisoformat(posted_at_iso)
    except ValueError:
        return "date de publication inconnue"
    age = datetime.now(timezone.utc) - posted_dt
    hours = age.total_seconds() / 3600
    if hours < 1:
        return f"publiée il y a {int(age.total_seconds() / 60)} min"
    if hours < 24:
        return f"publiée il y a {hours:.1f} h"
    return f"publiée il y a {hours / 24:.1f} j"


def _format_score_breakdown(breakdown: dict) -> list[str]:
    """Rend explicite comment le score a été calculé, pour ne jamais
    laisser le score comme une boîte noire dans la notification."""
    if not breakdown:
        return []
    if breakdown.get("reason") == "incomplete_estimation_potential_score":
        return [
            f"<i>Score de potentiel (pas encore de ventes réelles pour ce modèle/état) "
            f"— basé sur le prix affiché ({breakdown.get('listing_price_cad')} $ CA), "
            f"état : {breakdown.get('condition')}</i>"
        ]
    if "margin_score" not in breakdown:
        return []
    lines = [
        "<i>Détail : marge {ms}/45 · ROI {rs}/30 · fiabilité estimation {ec}/15 · "
        "fiabilité extraction {xc}/10</i>".format(
            ms=round(breakdown.get("margin_score", 0) * 0.45, 1),
            rs=round(breakdown.get("roi_score", 0) * 0.30, 1),
            ec=round(breakdown.get("estimation_confidence_score", 0) * 0.15, 1),
            xc=round(breakdown.get("extraction_confidence_score", 0) * 0.10, 1),
        )
    ]
    if breakdown.get("condition_penalty_applied"):
        lines.append("⚠️ <i>Pénalité appliquée (iCloud verrouillé ou dégâts d'eau)</i>")
    return lines


def _format_deal_message(listing: dict) -> str:
    n = listing.get("normalized", {})
    e = listing.get("estimation", {})
    price = e.get("listing_price_cad")
    resale = e.get("estimated_resale_cad")
    repair = e.get("estimated_repair_cad")
    margin = e.get("margin_cad")
    roi = e.get("roi_pct")

    lines = [f"🔥 <b>Score {listing.get('score')}/100</b>\n"]
    lines.append(f"<b>{n.get('model') or '?'}</b> — {n.get('condition')}")
    lines.append(f"Prix annonce : {price} $ CA")
    if resale is not None:
        lines.append(f"Revente estimée : {resale} $ CA")
        lines.append(f"Réparation estimée : {repair} $ CA")
        lines.append(f"Marge estimée : <b>{margin} $ CA</b> · ROI {roi}%")
    else:
        lines.append("ℹ️ Pas encore assez de ventes réelles pour ce modèle — score de potentiel.")

    breakdown_lines = _format_score_breakdown(listing.get("score_breakdown", {}))
    if breakdown_lines:
        lines.append("")
        lines.extend(breakdown_lines)

    lines.append(f"\n🕒 {_format_listing_age(listing.get('posted_at'))}")
    lines.append(f"📍 {listing.get('location')}")
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
# CYCLE DE SCAN — utilisé par le scan auto ET le scan manuel
# ---------------------------------------------------------

async def run_scan_cycle(context: ContextTypes.DEFAULT_TYPE, manual: bool = False, reply_message=None) -> dict:
    """Exécute un cycle complet : sources -> filtre pertinence -> estimation
    -> score -> stockage -> notification. Protégé par SCAN_LOCK pour éviter
    tout chevauchement entre scan auto et scan manuel."""
    if SCAN_LOCK.locked():
        if reply_message:
            await reply_message.reply_text("⏳ Un scan est déjà en cours, réessaie dans un instant.")
        return {}

    async with SCAN_LOCK:
        threshold = context.bot_data.get("score_threshold", DEFAULT_SCORE_THRESHOLD)

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
                    listing = score_listing(listing)

                    listing_id = insert_listing(listing)
                    if listing_id is None:
                        counters["duplicates"] += 1
                        continue

                    # Garde-fou final explicite avant tout envoi Telegram :
                    # ne jamais envoyer autre chose qu'un iPhone complet,
                    # même si une étape en amont a mal classé l'annonce.
                    if listing.get("relevance", {}).get("listing_type") != "whole_phone":
                        continue

                    if listing.get("score", 0) >= threshold and ALLOWED_CHAT_ID:
                        await context.bot.send_message(
                            chat_id=ALLOWED_CHAT_ID,
                            text=_format_deal_message(listing),
                            parse_mode=ParseMode.HTML,
                            reply_markup=_listing_keyboard(str(listing_id), listing["url"]),
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
                    "✅ <b>Scan manuel terminé</b>\n\n"
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
    """Commande /scan — déclenche un scan manuel immédiat."""
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
        ("setscore", "Modifier le seuil de score du scan auto"),
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
    application.add_handler(CommandHandler("setscore", set_score))
    application.add_handler(CommandHandler("aide", help_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(CommandHandler(["test", "calcul", "profit"], help_command))

    application.add_error_handler(error_handler)

    application.job_queue.run_repeating(scan_job, interval=CYCLE_SECONDS, first=15)

    LOGGER.info("Bot de rentabilité démarré. Scan auto toutes les %ds.", CYCLE_SECONDS)

    application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
