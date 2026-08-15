"""
pipeline/snipe.py

Système de "sniping" : détection + achat automatique des affaires qui
remplissent des critères stricts de sécurité et de rentabilité.

PRINCIPE DE SÉCURITÉ NUMÉRO UN : en cas de doute, ne JAMAIS acheter.
Toute condition manquante ou ambiguë fait échouer l'éligibilité au
snipe — l'annonce est alors simplement envoyée en notification
normale avec le bouton d'achat manuel, jamais perdue.

Garde-fous appliqués, dans l'ordre :
    1. Snipe désactivé -> refus immédiat
    2. Re-vérification anti-accessoire (défense en profondeur, même si
       le classificateur en amont a déjà normalement filtré ces cas)
    3. Prix inconnu -> refus
    4. Prix plancher de plausibilité (évite les erreurs d'affichage du
       type "2€" qui ne peuvent pas être un vrai iPhone complet)
    5. Prix plafond configuré par l'utilisateur (garde-fou dur)
    6. Marge estimée réelle disponible (champ margin_eur de
       pipeline/estimate.py) ET supérieure au seuil configuré — si
       aucune statistique de revente n'existe pour ce modèle/état
       (model_price_stats vide), le snipe est automatiquement refusé
    7. Confiance de l'estimation = "high" obligatoire, c'est-à-dire au
       moins 3 ventes comparables en base (MIN_SAMPLE_SIZE_TRUSTED côté
       estimate.py) — une marge calculée sur 1 ou 2 ventes seulement
       n'est PAS jugée assez fiable pour un achat automatique, même si
       elle dépasse le seuil configuré. C'est un garde-fou non
       désactivable : la précision prime sur la vitesse de snipe.
    8. ROI minimum (roi_pct), si configuré
    9. Modèle dans la liste blanche, si une liste blanche est définie
    10. Plafond de dépense quotidienne non dépassé

Le mode "dry-run" (simulation) est actif par défaut au niveau de
l'appelant (main.py) : aucun achat réel n'est déclenché tant que
l'utilisateur n'a pas explicitement activé le mode réel avec
/snipe live confirme.
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from pipeline.relevance import ACCESSORY_PATTERNS, _compile, _normalize

logger = logging.getLogger(__name__)

# Prix plancher de plausibilité — en dessous, ce n'est presque
# certainement pas un iPhone complet réel (accessoire mal classé,
# erreur de prix affiché, arnaque). Volontairement conservateur.
MIN_PLAUSIBLE_PRICE_EUR = Decimal("25")

_ACCESSORY_SAFETY_RE = _compile(ACCESSORY_PATTERNS)


@dataclass
class SnipeConfig:
    enabled: bool = False
    live_mode: bool = False  # False = simulation, True = achats réels
    max_price_eur: Optional[Decimal] = None
    min_margin_eur: Optional[Decimal] = None
    min_roi_percent: Optional[Decimal] = None
    allowed_models: Optional[set] = None  # None = tous les modèles autorisés
    daily_cap_eur: Optional[Decimal] = None
    spent_today_eur: Decimal = Decimal("0")
    spent_date: Optional[date] = None


def get_snipe_config(user_data: dict) -> SnipeConfig:
    raw = user_data.setdefault("snipe_config", {})
    today = date.today()
    if raw.get("spent_date") != today:
        raw["spent_date"] = today
        raw["spent_today_eur"] = Decimal("0")
    return SnipeConfig(
        enabled=raw.get("enabled", False),
        live_mode=raw.get("live_mode", False),
        max_price_eur=raw.get("max_price_eur"),
        min_margin_eur=raw.get("min_margin_eur"),
        min_roi_percent=raw.get("min_roi_percent"),
        allowed_models=raw.get("allowed_models"),
        daily_cap_eur=raw.get("daily_cap_eur"),
        spent_today_eur=raw.get("spent_today_eur", Decimal("0")),
        spent_date=raw.get("spent_date"),
    )


def save_snipe_config(user_data: dict, config: SnipeConfig) -> None:
    user_data["snipe_config"] = {
        "enabled": config.enabled,
        "live_mode": config.live_mode,
        "max_price_eur": config.max_price_eur,
        "min_margin_eur": config.min_margin_eur,
        "min_roi_percent": config.min_roi_percent,
        "allowed_models": config.allowed_models,
        "daily_cap_eur": config.daily_cap_eur,
        "spent_today_eur": config.spent_today_eur,
        "spent_date": config.spent_date,
    }


def record_spend(user_data: dict, amount_eur: Decimal) -> None:
    config = get_snipe_config(user_data)
    config.spent_today_eur += amount_eur
    save_snipe_config(user_data, config)


@dataclass
class SnipeDecision:
    eligible: bool
    reason: str


def evaluate_snipe(listing: dict, config: SnipeConfig) -> SnipeDecision:
    """Décide si une annonce doit être snipée. Retourne toujours une
    raison explicite, y compris en cas d'éligibilité, pour traçabilité
    dans les logs."""

    if not config.enabled:
        return SnipeDecision(False, "snipe désactivé")

    title = listing.get("title", "") or ""
    description = listing.get("description", "") or ""
    text = _normalize(f"{title} {description}")

    if _ACCESSORY_SAFETY_RE.search(text):
        return SnipeDecision(False, "mot-clé accessoire détecté (sécurité)")

    e = listing.get("estimation", {}) or {}
    price = e.get("listing_price_eur")
    if price is None:
        return SnipeDecision(False, "prix inconnu")
    price = Decimal(str(price))

    if price < MIN_PLAUSIBLE_PRICE_EUR:
        return SnipeDecision(False, f"prix sous le plancher de plausibilité ({MIN_PLAUSIBLE_PRICE_EUR} €)")

    if config.max_price_eur is not None and price > config.max_price_eur:
        return SnipeDecision(False, "prix au-dessus du plafond configuré")

    margin = e.get("margin_eur")
    if margin is None:
        return SnipeDecision(False, "aucune statistique de revente disponible pour ce modèle/état")
    margin = Decimal(str(margin))
    if config.min_margin_eur is not None and margin < config.min_margin_eur:
        return SnipeDecision(False, "marge estimée sous le seuil configuré")

    # Garde-fou non désactivable : moins de 3 ventes comparables en base
    # -> pas assez fiable pour un achat automatique.
    confidence = e.get("estimation_confidence")
    if confidence != "high":
        return SnipeDecision(
            False,
            f"confiance d'estimation insuffisante ({confidence or 'inconnue'}) — "
            "échantillon de ventes comparables trop petit",
        )

    if config.min_roi_percent is not None:
        roi = e.get("roi_pct")
        if roi is None:
            return SnipeDecision(False, "ROI non calculable (pas de données suffisantes)")
        if Decimal(str(roi)) < config.min_roi_percent:
            return SnipeDecision(False, "ROI estimé sous le seuil configuré")

    if config.allowed_models:
        n = listing.get("normalized", {}) or {}
        model = n.get("model")
        if not model or model not in config.allowed_models:
            return SnipeDecision(False, "modèle hors liste blanche")

    if config.daily_cap_eur is not None:
        if config.spent_today_eur + price > config.daily_cap_eur:
            return SnipeDecision(False, "plafond de dépense quotidienne atteint")

    return SnipeDecision(True, "critères remplis")
