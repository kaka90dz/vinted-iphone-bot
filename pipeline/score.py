"""
pipeline/score.py — bot Vinted (EUR)

Calcule un score sur 100 à partir de l'estimation (pipeline/estimate.py).
Le score n'est jamais une boîte noire : le breakdown est stocké avec, pour
que tu puisses comprendre et ajuster les pondérations dans le temps.

Garde-fous explicites (identiques à iphone-deals-bot) :
- Une annonce non pertinente (accessoire, pièce, service...) ne reçoit
  JAMAIS de note : score forcé à 0, quelle que soit l'estimation.
- Une estimation incomplète (pas de stats historiques) ne donne PAS
  automatiquement 0 : elle reçoit un "score de potentiel" basé sur le
  prix affiché, pour ne pas noyer les affaires sur des modèles neufs
  dans le catalogue.
- iCloud verrouillé et dégâts d'eau sont fortement pénalisés (le
  téléphone peut être invendable ou très coûteux à réparer).

Différence vs iphone-deals-bot : cibles de marge/ROI et référence de
prix ajustées au marché Vinted France (prix généralement plus bas que
Facebook Marketplace Montréal) — à retoucher toi-même une fois que tu
as assez de données réelles (table operations) pour comparer.
"""

MARGIN_TARGET_EUR = 100
ROI_TARGET_PCT = 80

CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.6, "low": 0.25}

# Score de potentiel accordé quand aucune estimation de revente n'est
# disponible mais que l'annonce est un iPhone complet à bas prix.
POTENTIAL_SCORE_BASE = 30
POTENTIAL_PRICE_REFERENCE_EUR = 180  # au-delà, le "potentiel" décroît

# Conditions fortement pénalisées : multiplicateur appliqué au score final.
HEAVY_PENALTY_CONDITIONS = {
    "icloud_locked": 0.15,
    "water_damage": 0.35,
}


def _clamp(value: float, low: float = 0, high: float = 100) -> float:
    return max(low, min(high, value))


def score_listing(listing: dict) -> dict:
    """Ajoute 'score' (float 0-100) et 'score_breakdown' (dict) à l'annonce."""
    relevance = listing.get("relevance", {})

    # Garde-fou n°1 : jamais de note pour une annonce non pertinente.
    if not relevance.get("is_relevant", True):
        listing["score"] = 0.0
        listing["score_breakdown"] = {
            "reason": "not_relevant",
            "listing_type": relevance.get("listing_type"),
        }
        return listing

    estimation = listing.get("estimation", {})
    normalized = listing.get("normalized", {})
    condition = normalized.get("condition", "unknown")

    margin = estimation.get("margin_eur")
    roi = estimation.get("roi_pct")

    if margin is None or roi is None:
        # Garde-fou n°2 : estimation incomplète != 0. Score de potentiel
        # basé sur le prix affiché quand on l'a, sinon score plancher.
        price = estimation.get("listing_price_eur")
        if price is not None and price > 0:
            price_factor = _clamp(1 - (price / POTENTIAL_PRICE_REFERENCE_EUR), 0, 1)
            potential = POTENTIAL_SCORE_BASE + (100 - POTENTIAL_SCORE_BASE) * 0.3 * price_factor
        else:
            potential = POTENTIAL_SCORE_BASE * 0.5

        potential = _apply_condition_penalty(potential, condition)

        listing["score"] = round(potential, 1)
        listing["score_breakdown"] = {
            "reason": "incomplete_estimation_potential_score",
            "listing_price_eur": price,
            "condition": condition,
        }
        return listing

    margin_score = _clamp(100 * margin / MARGIN_TARGET_EUR)
    roi_score = _clamp(100 * roi / ROI_TARGET_PCT)

    estimation_confidence_score = 100 * CONFIDENCE_WEIGHT.get(
        estimation.get("estimation_confidence", "low"), 0.25
    )
    extraction_confidence_score = 100 * CONFIDENCE_WEIGHT.get(
        normalized.get("confidence", "low"), 0.25
    )

    final_score = (
        0.45 * margin_score
        + 0.30 * roi_score
        + 0.15 * estimation_confidence_score
        + 0.10 * extraction_confidence_score
    )

    if margin < 0:
        final_score = min(final_score, 15)

    final_score = _apply_condition_penalty(final_score, condition)

    listing["score"] = round(final_score, 1)
    listing["score_breakdown"] = {
        "margin_eur": margin,
        "roi_pct": roi,
        "margin_score": round(margin_score, 1),
        "roi_score": round(roi_score, 1),
        "estimation_confidence_score": round(estimation_confidence_score, 1),
        "extraction_confidence_score": round(extraction_confidence_score, 1),
        "condition_penalty_applied": condition in HEAVY_PENALTY_CONDITIONS,
    }
    return listing


def _apply_condition_penalty(score: float, condition: str) -> float:
    multiplier = HEAVY_PENALTY_CONDITIONS.get(condition)
    if multiplier is None:
        return score
    return round(score * multiplier, 1)
