"""
pipeline/normalize.py

Étape 1 : classification de pertinence (pipeline/relevance.py) — TOUJOURS
en premier, avant toute tentative de détection de modèle. Une annonce
rejetée (accessoire, pièce, service, acheteur, catalogue, autre marque,
ambiguë) n'est jamais envoyée à Anthropic : ça évite de payer pour
classifier des étuis.

Étape 2 (seulement si whole_phone) : extraction du modèle/état/batterie.
Deux modes, contrôlés par la variable d'environnement
USE_ANTHROPIC_NORMALIZER :
    - "true"  : appelle Claude Haiku pour une extraction fine.
    - "false" (défaut recommandé actuellement) : extraction locale par
      règles simples, gratuite et suffisante pour la plupart des cas.

Garde-fou crédit Anthropic : si un appel échoue avec une erreur de
crédit insuffisant (HTTP 400 "credit balance is too low"), le module
désactive automatiquement les appels Anthropic pour le reste du process
(ANTHROPIC_DISABLED=True), logue une seule fois, et bascule sur
l'extraction locale pour tout le reste du scan — sans jamais faire
planter le pipeline.

Identique à la version iphone-deals-bot — la logique d'extraction ne
dépend pas de la source (Facebook ou Vinted). Pour Vinted, description
est souvent vide (non exposée par la recherche catalogue), donc
l'extraction se base surtout sur le titre.
"""

import os
import re
import json
import logging
from typing import Optional

from pipeline.relevance import classify_listing
from pipeline.vocab import KNOWN_MODELS, KNOWN_CONDITIONS

logger = logging.getLogger(__name__)

USE_ANTHROPIC_NORMALIZER = os.environ.get("USE_ANTHROPIC_NORMALIZER", "false").lower() == "true"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

# État global du module : une fois True, on ne retente plus jamais
# Anthropic pour le reste du process (jusqu'au prochain redéploiement).
ANTHROPIC_DISABLED = False
_ANTHROPIC_DISABLED_LOGGED = False

_client = None


def _get_anthropic_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    return _client


_MODEL_LOOKUP = sorted(KNOWN_MODELS, key=len, reverse=True)


def _strip_json_fences(text: str) -> str:
    return re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()


def _local_extract_model(text: str) -> Optional[str]:
    """Détection de modèle par correspondance simple contre KNOWN_MODELS,
    insensible à la casse et aux espaces multiples."""
    normalized = re.sub(r"\s+", " ", text.lower())
    for model in _MODEL_LOOKUP:
        if model.lower() in normalized:
            return model
    return None


# Mots-clés -> condition standardisée (utilisé par l'extraction locale).
_CONDITION_KEYWORDS = [
    (r"icloud", "icloud_locked"),
    (r"water\s*damage|degat.*eau|dommage.*eau", "water_damage"),
    (r"ne s.?allume plus|doesn.?t turn on|no power|won.?t turn on", "no_power"),
    (r"ecran|screen|cracked|fissure|casse", "cracked_screen"),
    (r"batterie|battery", "battery_issue"),
    (r"charg(e|ing)|port de charge|charging port", "charging_issue"),
    (r"bloque|locked|carrier lock", "carrier_locked"),
    (r"pour piece|for parts|piece[s]? seulement", "for_parts"),
    (r"fonctionne|functional|fully functional|powers on|marche bien", "functional"),
]


def _local_extract_condition(text: str) -> str:
    normalized = text.lower()
    for pattern, condition in _CONDITION_KEYWORDS:
        if re.search(pattern, normalized):
            return condition
    return "unknown"


def _local_extract_battery(text: str) -> Optional[int]:
    match = re.search(r"batterie\D{0,10}(\d{1,3})\s*%|battery\D{0,10}(\d{1,3})\s*%", text.lower())
    if match:
        value = match.group(1) or match.group(2)
        try:
            pct = int(value)
            return pct if 0 <= pct <= 100 else None
        except ValueError:
            return None
    return None


def _local_extract_storage(text: str) -> Optional[int]:
    match = re.search(r"(\d{2,4})\s*(?:go|gb)\b", text.lower())
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


def _local_normalize(title: str, description: str) -> dict:
    """Extraction locale, gratuite, sans appel réseau."""
    text = f"{title} {description}"
    model = _local_extract_model(text)
    return {
        "model": model,
        "condition": _local_extract_condition(text),
        "battery_health": _local_extract_battery(text),
        "storage_gb": _local_extract_storage(text),
        "price": None,  # extrait séparément dans pipeline/estimate.py
        "confidence": "medium" if model else "low",
    }


SYSTEM_PROMPT = f"""Tu extrais des données structurées à partir d'annonces
de vente d'iPhone d'occasion (Vinted, etc.), souvent rédigées en français,
parfois avec des fautes ou du jargon.

Réponds UNIQUEMENT avec un objet JSON valide, sans texte avant ni après,
sans balises markdown, avec exactement ces clés :

- "model": un identifiant EXACT parmi cette liste (ou null si le modèle
  n'est pas identifiable avec confiance) : {json.dumps(KNOWN_MODELS)}
- "condition": un identifiant EXACT parmi cette liste : {json.dumps(KNOWN_CONDITIONS)}
- "battery_health": un entier (pourcentage) si mentionné, sinon null
- "storage_gb": un entier si mentionné, sinon null
- "price": un nombre si extractible, sinon null
- "confidence": "high", "medium" ou "low"

N'invente jamais une information absente. Si un doute existe, mets null ou "unknown"."""


def _anthropic_extract(title: str, description: str, price_raw) -> Optional[dict]:
    """Tente l'extraction via Claude Haiku. Retourne None si Anthropic est
    désactivé ou si l'appel échoue pour une raison autre que le crédit
    (dans ce dernier cas, l'appelant doit basculer sur l'extraction locale)."""
    global ANTHROPIC_DISABLED, _ANTHROPIC_DISABLED_LOGGED

    if ANTHROPIC_DISABLED:
        return None

    import anthropic

    user_content = (
        f"Titre: {title}\nDescription: {description}\nPrix affiché (brut): {price_raw}"
    )

    try:
        client = _get_anthropic_client()
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        raw_text = response.content[0].text
        parsed = json.loads(_strip_json_fences(raw_text))

        if parsed.get("model") not in KNOWN_MODELS:
            parsed["model"] = None
        if parsed.get("condition") not in KNOWN_CONDITIONS:
            parsed["condition"] = "unknown"
        return parsed

    except anthropic.APIStatusError as exc:
        body_text = str(getattr(exc, "message", "") or str(exc)).lower()
        is_credit_issue = exc.status_code == 400 and "credit balance" in body_text
        if is_credit_issue:
            ANTHROPIC_DISABLED = True
            if not _ANTHROPIC_DISABLED_LOGGED:
                logger.error(
                    "Anthropic désactivé pour le reste du process : crédit "
                    "insuffisant (HTTP 400). Bascule sur l'extraction locale."
                )
                _ANTHROPIC_DISABLED_LOGGED = True
        else:
            logger.exception("Erreur Anthropic non liée au crédit, annonce ignorée par le LLM: %s", title)
        return None

    except (json.JSONDecodeError, IndexError, Exception):
        logger.exception("Échec extraction Anthropic pour l'annonce: %s", title)
        return None


def normalize_listing(listing: dict) -> dict:
    """
    Ajoute au dict de l'annonce :
    - "relevance": le résultat complet de la classification (dict)
    - "normalized": modèle/état extraits (uniquement rempli si whole_phone)

    Les annonces non pertinentes reçoivent un "normalized" minimal
    (tout à None/unknown) — elles ne doivent jamais être scorées ni
    envoyées, mais on garde le dict cohérent pour le reste du pipeline.
    """
    title = listing.get("title", "")
    description = listing.get("description", "")
    price_raw = listing.get("price_raw", "")

    relevance = classify_listing(title, description)
    listing["relevance"] = relevance.as_dict()

    if not relevance.is_relevant:
        logger.info(
            "Annonce rejetée [%s / %s] : %s",
            relevance.listing_type, relevance.rejection_reason, title,
        )
        listing["normalized"] = {
            "model": None,
            "condition": "unknown",
            "battery_health": None,
            "storage_gb": None,
            "price": None,
            "confidence": "high",
        }
        return listing

    parsed = None
    if USE_ANTHROPIC_NORMALIZER and not ANTHROPIC_DISABLED:
        parsed = _anthropic_extract(title, description, price_raw)

    if parsed is None:
        parsed = _local_normalize(title, description)

    listing["normalized"] = parsed
    return listing


def normalize_batch(listings: list[dict]) -> list[dict]:
    """Normalise une liste d'annonces. Les échecs individuels n'arrêtent pas le lot."""
    normalized = []
    for listing in listings:
        try:
            normalized.append(normalize_listing(listing))
        except Exception:
            logger.exception("Erreur inattendue sur l'annonce: %s", listing.get("title"))
    return normalized


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    examples = [
        {"title": "iphone13 promax ecran fissure batterie 85%", "description": "fonctionne bien sinon", "price_raw": "180"},
        {"title": "Étui iPhone 11 Kaseme", "description": "", "price_raw": "15"},
    ]
    for ex in examples:
        print(json.dumps(normalize_listing(ex), indent=2, ensure_ascii=False))
