"""
pipeline/estimate.py — bot Vinted (EUR)

Prend une annonce normalisée (sortie de pipeline/normalize.py) et calcule :
- une estimation de la valeur de revente (basée sur model_price_stats, à
  défaut sur une table de repli statique)
- une estimation du coût de réparation (idem)
- la marge et le ROI attendus si tu achètes au prix affiché

Ne fait AUCUNE requête base de données si l'annonce n'est pas un iPhone
complet (accessoire, pièce, service...) — évite du travail inutile.

Différence vs iphone-deals-bot : tout est en EUR (marché Vinted France
uniquement), et les coûts de réparation de repli sont ceux d'un marché
français plutôt que canadien.
"""

import logging
import re
from typing import Optional

from storage.db import get_connection

logger = logging.getLogger(__name__)

# Coûts de réparation de repli (EUR), utilisés quand aucune statistique
# réelle n'existe encore pour model_price_stats. À ajuster avec tes
# propres coûts observés au fil du temps.
FALLBACK_REPAIR_COST_EUR = {
    "cracked_screen": 60,
    "battery_issue": 35,
    "charging_issue": 25,
    "carrier_locked": 0,
    "for_parts": 0,
    "water_damage": 80,
    "no_power": 70,
    "icloud_locked": 0,
    "functional": 0,
    "unknown": 50,
}

MIN_SAMPLE_SIZE_TRUSTED = 3


def _extract_price(price_raw) -> Optional[float]:
    if price_raw is None:
        return None
    match = re.search(r"(\d+(?:[.,]\d{1,2})?)", str(price_raw).replace(",", "."))
    return float(match.group(1)) if match else None


def _lookup_stats(model: str, condition: str) -> Optional[dict]:
    query = """
        SELECT avg_resale_eur, avg_repair_eur, sample_size
        FROM model_price_stats
        WHERE model = %s AND condition = %s;
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (model, condition))
            return cur.fetchone()


def _empty_estimation() -> dict:
    return {
        "listing_price_eur": None,
        "estimated_resale_eur": None,
        "estimated_repair_eur": None,
        "margin_eur": None,
        "roi_pct": None,
        "estimation_confidence": "low",
    }


def estimate_listing(listing: dict) -> dict:
    """Ajoute une clé "estimation". Les annonces non pertinentes (whole_phone
    uniquement passe cette étape) reçoivent une estimation vide sans
    toucher la base de données."""
    relevance = listing.get("relevance", {})
    if not relevance.get("is_relevant", True):
        listing["estimation"] = _empty_estimation()
        return listing

    normalized = listing.get("normalized", {})
    model = normalized.get("model")
    condition = normalized.get("condition", "unknown")

    listing_price = normalized.get("price") or _extract_price(listing.get("price_raw"))

    estimation = _empty_estimation()
    estimation["listing_price_eur"] = listing_price

    if not model or listing_price is None:
        listing["estimation"] = estimation
        return listing

    stats = _lookup_stats(model, condition)

    if stats and stats.get("avg_resale_eur") is not None:
        estimation["estimated_resale_eur"] = float(stats["avg_resale_eur"])
        estimation["estimated_repair_eur"] = float(
            stats["avg_repair_eur"] or FALLBACK_REPAIR_COST_EUR.get(condition, 50)
        )
        estimation["estimation_confidence"] = (
            "high" if stats["sample_size"] >= MIN_SAMPLE_SIZE_TRUSTED else "medium"
        )
    else:
        estimation["estimated_repair_eur"] = FALLBACK_REPAIR_COST_EUR.get(condition, 50)
        logger.info("Pas de stats pour %s / %s — revente non estimée.", model, condition)

    if estimation["estimated_resale_eur"] is not None:
        margin = (
            estimation["estimated_resale_eur"]
            - listing_price
            - estimation["estimated_repair_eur"]
        )
        cost_basis = listing_price + estimation["estimated_repair_eur"]
        estimation["margin_eur"] = round(margin, 2)
        estimation["roi_pct"] = round(100 * margin / cost_basis, 2) if cost_basis > 0 else None

    listing["estimation"] = estimation
    return listing
