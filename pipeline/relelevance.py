"""
pipeline/relevance.py

Source UNIQUE de vérité pour décider si une annonce vend réellement un
iPhone complet (fonctionnel, écran cassé, batterie à changer, ne s'allume
plus, pour pièces, tel quel) — par opposition à un accessoire, une pièce
détachée, un service de réparation, une annonce d'acheteur, un catalogue
multi-modèles, ou une autre marque.

Identique à la version iphone-deals-bot — les règles sont déjà en
français/anglais et ne dépendent pas de la source (Facebook ou Vinted).

Principe central : les règles de REJET sont évaluées AVANT toute
détection de modèle. "Étui iPhone 11" contient un modèle, mais "étui"
doit gagner.

Ordre d'évaluation (fixe, ne pas réordonner sans mettre à jour les tests) :
    1. acheteur professionnel      -> buyer_ad
    2. réparateur ou service       -> repair_service
    3. accessoire                  -> accessory
    4. pièce seule / kit           -> part_only
    5. catalogue / plusieurs modèles -> shop_catalog
    6. autre marque                -> other_brand
    7. aucune mention "iPhone"     -> ambiguous
    8. sinon                       -> whole_phone
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, asdict
from typing import Optional


# ---------------------------------------------------------------------
# Normalisation de texte : minuscules, accents retirés, guillemets
# courbes uniformisés. Toutes les listes de mots-clés ci-dessous sont
# déjà écrites en version "normalisée" (sans accents).
# ---------------------------------------------------------------------

def _normalize(text: Optional[str]) -> str:
    if not text:
        return ""
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower()


def _compile(patterns: list[str]) -> re.Pattern:
    """Compile une liste de mots-clés/phrases en un seul pattern avec
    frontières de mot, pour éviter les faux positifs sur des sous-chaînes
    (ex: 'changer' ne doit pas matcher 'charger')."""
    escaped = sorted((re.escape(p) for p in patterns), key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b")


# ---------------------------------------------------------------------
# VOCABULAIRE — une entrée par catégorie de rejet, dans l'ordre du brief.
# Toutes les chaînes sont déjà sans accents (comparées à du texte normalisé).
# ---------------------------------------------------------------------

BUYER_PATTERNS = [
    "j'achete vos iphone", "jachete vos iphone", "nous achetons",
    "paiement immediat", "we buy iphones", "i buy iphones",
    "cash for phones", "meilleur prix", "estimation gratuite",
    "wanted iphone",
]

SERVICE_PATTERNS = [
    "reparation", "reparateur", "service de reparation", "nous reparons",
    "je repare", "on repare", "remplacement ecran",
    "screen replacement service", "battery replacement service",
    "installation incluse", "diagnostic gratuit", "rendez-vous",
    "technicien", "atelier", "phone sales & repair",
    "phone sales and repair", "cellfix", "cell fix", "repair shop",
]

ACCESSORY_PATTERNS = [
    "etui", "coque", "housse", "case", "phone case", "wallet case",
    "flip case", "cover", "bumper", "quad lock", "protecteur ecran",
    "protecteur d'ecran", "screen protector", "verre trempe",
    "tempered glass", "vitre protectrice", "glass clear",
    "support telephone", "phone holder", "car mount", "chargeur",
    "charger", "cable", "charging cable", "adaptateur", "2 pack",
    "3 pack", "pack of 2",
]

PARTS_PATTERNS = [
    "repair kit", "kit de reparation", "tool kit", "kit d'outils",
    "opening tool", "replacement screen", "replacement display",
    "screen assembly", "display assembly", "lcd", "oled", "digitizer",
    "replacement battery", "batterie neuve", "charging port",
    "lightning port", "connecteur de charge", "port de charge",
    "back glass", "vitre arriere", "camera module", "face id module",
    "motherboard", "logic board", "carte mere", "nappe", "flex cable",
    "chassis", "housing", "lot de pieces", "pieces detachees",
    "for iphone", "pour iphone", "compatible avec iphone",
]

CATALOG_PATTERNS = [
    "tous modeles disponibles", "all models available",
    "plusieurs modeles", "prix selon modele", "prix a partir de",
    "stock disponible", "wholesale", "bulk",
]

OTHER_BRAND_PATTERNS = [
    "samsung", "google pixel", "pixel", "oneplus", "huawei",
    "motorola", "xiaomi", "nokia", "lg",
]

# Preuves positives : ne servent qu'à augmenter la confiance, jamais à
# rejeter. Une annonce peut être acceptée sans aucune de ces preuves.
POSITIVE_PATTERNS = [
    "a vendre", "for sale", "telephone", "phone", "cellulaire",
    "appareil", "device", "fonctionne", "fully functional",
    "powers on", "ecran casse", "cracked screen", "ne s'allume plus",
    "doesn't turn on", "doesnt turn on", "batterie a changer",
    "battery issue", "vendu tel quel", "as is", "pour pieces",
    "for parts", "icloud lock", "no face id", "camera defectueuse",
]

MODEL_MENTION_RE = re.compile(r"\b(?:apple\s+)?iphone\b")

_BUYER_RE = _compile(BUYER_PATTERNS)
_SERVICE_RE = _compile(SERVICE_PATTERNS)
_ACCESSORY_RE = _compile(ACCESSORY_PATTERNS)
_PARTS_RE = _compile(PARTS_PATTERNS)
_CATALOG_RE = _compile(CATALOG_PATTERNS)
_OTHER_BRAND_RE = _compile(OTHER_BRAND_PATTERNS)
_POSITIVE_RE = _compile(POSITIVE_PATTERNS)


@dataclass
class ClassificationResult:
    is_relevant: bool
    listing_type: str  # whole_phone | accessory | part_only | repair_service
                        # | buyer_ad | shop_catalog | other_brand | ambiguous
    rejection_reason: Optional[str]
    matched_rule: str
    confidence: str  # "high" | "medium" | "low"

    def as_dict(self) -> dict:
        return asdict(self)


def _slug(match_text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", match_text.upper()).strip("_")


def classify_listing(title: str, description: str = "") -> ClassificationResult:
    """Classifie une annonce. Évalue les règles de rejet dans l'ordre
    fixe du brief, avant toute tentative de détection de modèle."""
    text = _normalize(f"{title} {description}")

    m = _BUYER_RE.search(text)
    if m:
        return ClassificationResult(
            is_relevant=False,
            listing_type="buyer_ad",
            rejection_reason="buyer_ad",
            matched_rule=f"BUYER_{_slug(m.group(0))}",
            confidence="high",
        )

    m = _SERVICE_RE.search(text)
    if m:
        return ClassificationResult(
            is_relevant=False,
            listing_type="repair_service",
            rejection_reason="repair_service",
            matched_rule=f"SERVICE_{_slug(m.group(0))}",
            confidence="high",
        )

    m = _ACCESSORY_RE.search(text)
    if m:
        return ClassificationResult(
            is_relevant=False,
            listing_type="accessory",
            rejection_reason="phone_case" if m.group(0) in (
                "etui", "coque", "housse", "case", "phone case",
                "wallet case", "flip case", "cover", "bumper", "quad lock",
            ) else "accessory",
            matched_rule=f"ACCESSORY_{_slug(m.group(0))}",
            confidence="high",
        )

    m = _PARTS_RE.search(text)
    if m:
        return ClassificationResult(
            is_relevant=False,
            listing_type="part_only",
            rejection_reason="part_repair_kit" if "kit" in m.group(0) or "tool" in m.group(0) else "part_only",
            matched_rule=f"PART_{_slug(m.group(0))}",
            confidence="high",
        )

    m = _CATALOG_RE.search(text)
    if m:
        return ClassificationResult(
            is_relevant=False,
            listing_type="shop_catalog",
            rejection_reason="shop_catalog",
            matched_rule=f"CATALOG_{_slug(m.group(0))}",
            confidence="high",
        )

    m = _OTHER_BRAND_RE.search(text)
    if m:
        return ClassificationResult(
            is_relevant=False,
            listing_type="other_brand",
            rejection_reason="other_brand",
            matched_rule=f"BRAND_{_slug(m.group(0))}",
            confidence="high",
        )

    if not MODEL_MENTION_RE.search(text):
        return ClassificationResult(
            is_relevant=False,
            listing_type="ambiguous",
            rejection_reason="no_iphone_mention",
            matched_rule="MODEL_ABSENT",
            confidence="high",
        )

    has_positive = bool(_POSITIVE_RE.search(text))
    return ClassificationResult(
        is_relevant=True,
        listing_type="whole_phone",
        rejection_reason=None,
        matched_rule="WHOLE_PHONE_ACCEPT",
        confidence="high" if has_positive else "medium",
    )
