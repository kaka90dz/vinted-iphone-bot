"""
pipeline/relevance.py

Source de vérité pour décider si une annonce vend réellement un iPhone
complet — par opposition à un accessoire, une annonce d'acheteur pro,
une autre marque, ou une annonce ambiguë (aucune mention iPhone).

Version bot Vinted : les catégories "pièce détachée" (part_only),
"service/réparateur" (repair_service) et "catalogue de vendeur pro"
(shop_catalog) sont volontairement DÉSACTIVÉES — ces situations
n'existent quasiment pas sur Vinted (contrairement à Facebook
Marketplace où des ateliers de réparation et revendeurs pro postent
des annonces). Les listes de mots-clés PARTS_PATTERNS, SERVICE_PATTERNS
et CATALOG_PATTERNS restent définies ci-dessous pour référence/débogage,
mais ne sont plus utilisées dans classify_listing().

Ordre d'évaluation actif (fixe, ne pas réordonner sans mettre à jour
les tests) :
    1. acheteur professionnel      -> buyer_ad
    2. accessoire                  -> accessory
    3. autre marque                -> other_brand
    4. aucune mention "iPhone"     -> ambiguous
    5. sinon                       -> whole_phone
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, asdict
from typing import Optional


def _normalize(text: Optional[str]) -> str:
    if not text:
        return ""
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower()


def _compile(patterns: list[str]) -> re.Pattern:
    escaped = sorted((re.escape(p) for p in patterns), key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b")


# ---------------------------------------------------------------------
# VOCABULAIRE — patterns actifs + patterns conservés pour référence
# (part_only / repair_service / shop_catalog ne sont plus appliqués)
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
_ACCESSORY_RE = _compile(ACCESSORY_PATTERNS)
_OTHER_BRAND_RE = _compile(OTHER_BRAND_PATTERNS)
_POSITIVE_RE = _compile(POSITIVE_PATTERNS)
# _SERVICE_RE, _PARTS_RE, _CATALOG_RE ne sont plus compilés/utilisés —
# ces filtres sont désactivés sur le bot Vinted (voir docstring).


@dataclass
class ClassificationResult:
    is_relevant: bool
    listing_type: str
    rejection_reason: Optional[str]
    matched_rule: str
    confidence: str

    def as_dict(self) -> dict:
        return asdict(self)


def _slug(match_text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", match_text.upper()).strip("_")


def classify_listing(title: str, description: str = "") -> ClassificationResult:
    """Classifie une annonce. part_only / repair_service / shop_catalog
    sont désactivés (voir docstring du module) — seuls buyer_ad,
    accessory, other_brand et ambiguous peuvent rejeter une annonce."""
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
