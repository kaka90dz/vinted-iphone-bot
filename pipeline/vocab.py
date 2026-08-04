"""
pipeline/vocab.py

Vocabulaire fermé utilisé pour contraindre l'extraction (locale ou LLM).
Garder cette liste à jour = garantir que toutes tes annonces normalisées
utilisent les mêmes identifiants, ce qui rend les comparaisons de prix
et les statistiques fiables dans le temps.

Identique à la version utilisée par iphone-deals-bot — le vocabulaire de
modèles/états d'iPhone ne dépend pas de la source (Facebook ou Vinted).
"""

# Identifiants de modèles utilisés partout dans la base (stockage, stats,
# estimation de valeur). Ajouter une entrée ici avant qu'un nouveau modèle
# soit ciblé par le bot. NOTE: la pertinence (pipeline/relevance.py) ne
# dépend PAS de cette liste — elle accepte tout iPhone, y compris des
# modèles plus anciens (ex: iPhone 5S) qui ne sont pas encore ici.
KNOWN_MODELS = [
    "iPhone 5S",
    "iPhone 6", "iPhone 6 Plus", "iPhone 6S", "iPhone 6S Plus",
    "iPhone 7", "iPhone 7 Plus",
    "iPhone 8", "iPhone 8 Plus",
    "iPhone X",
    "iPhone XR",
    "iPhone XS",
    "iPhone XS Max",
    "iPhone 11",
    "iPhone 11 Pro",
    "iPhone 11 Pro Max",
    "iPhone 12",
    "iPhone 12 Mini",
    "iPhone 12 Pro",
    "iPhone 12 Pro Max",
    "iPhone 13",
    "iPhone 13 Mini",
    "iPhone 13 Pro",
    "iPhone 13 Pro Max",
    "iPhone 14",
    "iPhone 14 Plus",
    "iPhone 14 Pro",
    "iPhone 14 Pro Max",
    "iPhone 15",
    "iPhone 15 Plus",
    "iPhone 15 Pro",
    "iPhone 15 Pro Max",
    "iPhone 16",
    "iPhone 16 Plus",
    "iPhone 16 Pro",
    "iPhone 16 Pro Max",
    "iPhone SE (2nd generation)",
    "iPhone SE (3rd generation)",
]

# États/problèmes standardisés. "unknown" = l'annonce ne précise rien de
# clair, à ne pas confondre avec "functional" (annonce qui dit explicitement
# que ça fonctionne).
KNOWN_CONDITIONS = [
    "cracked_screen",
    "battery_issue",
    "charging_issue",
    "carrier_locked",
    "for_parts",
    "water_damage",
    "no_power",
    "icloud_locked",
    "functional",
    "unknown",
]

# Types de classification retournés par pipeline/relevance.py.
# Seul "whole_phone" doit continuer dans le pipeline d'estimation/score.
LISTING_TYPES = [
    "whole_phone",
    "accessory",
    "part_only",
    "repair_service",
    "buyer_ad",
    "shop_catalog",
    "other_brand",
    "ambiguous",
]
