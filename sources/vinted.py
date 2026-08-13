"""
sources/vinted.py

Récupère les annonces Vinted (France) via la lib `vinted-scraper`.

Deux catégories de recherche, sélectionnables depuis le menu Telegram
(voir main.py) :
    - "broken" (défaut) : iPhone cassés, bloqués iCloud, pour pièces
    - "all"             : tous les iPhone, sans restriction d'état

CORRECTIF IMPORTANT : item.photo n'est pas toujours une simple chaîne
d'URL — selon la version de la lib, c'est un objet structuré avec ses
propres attributs (url, full_size_url, etc.). Envoyer cet objet tel
quel à Telegram produit l'erreur "wrong port number specified in the
url" car Telegram reçoit une représentation texte de l'objet Python
au lieu d'une vraie URL. _extract_photo_url() gère les deux cas.
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional

from vinted_scraper import VintedScraper

logger = logging.getLogger(__name__)

VINTED_BASE_URL = os.environ.get("VINTED_BASE_URL", "https://www.vinted.fr")
VINTED_PRICE_MAX = os.environ.get("VINTED_PRICE_MAX")  # optionnel, en EUR
VINTED_RESULTS_PER_SEARCH = int(os.environ.get("VINTED_RESULTS_PER_SEARCH", "20"))

BROKEN_SEARCHES = [
    {"query": "iphone cassé"},
    {"query": "iphone écran fissuré"},
    {"query": "iphone hs"},
    {"query": "iphone pour pièces"},
    {"query": "iphone ne s'allume plus"},
    {"query": "iphone icloud"},
]

ALL_SEARCHES = [
    {"query": "iphone 11"},
    {"query": "iphone 12"},
    {"query": "iphone 13"},
    {"query": "iphone 14"},
    {"query": "iphone 15"},
    {"query": "iphone 16"},
    {"query": "iphone se"},
    {"query": "iphone xr"},
    {"query": "iphone xs"},
]

CATEGORIES = {
    "broken": BROKEN_SEARCHES,
    "all": ALL_SEARCHES,
}

CATEGORY_LABELS = {
    "broken": "iPhone cassé / bloqué / pour pièces",
    "all": "Tous les iPhone",
}

_current_category = "broken"


def set_category(category: str) -> None:
    global _current_category
    if category not in CATEGORIES:
        raise ValueError(f"Catégorie inconnue: {category}")
    _current_category = category


def get_current_category() -> str:
    return _current_category


def get_current_category_label() -> str:
    return CATEGORY_LABELS.get(_current_category, _current_category)


class ScanStats:
    def __init__(self):
        self.found_raw = 0
        self.accepted_source = 0
        self.rejected_too_old = 0
        self.rejected_too_far = 0
        self.errors = 0

    def as_dict(self) -> dict:
        return {
            "found_raw": self.found_raw,
            "accepted_source": self.accepted_source,
            "rejected_too_old": self.rejected_too_old,
            "rejected_too_far": self.rejected_too_far,
            "errors": self.errors,
        }


_last_stats = ScanStats()


def get_last_stats() -> dict:
    return _last_stats.as_dict()


def _extract_photo_url(photo_obj) -> Optional[str]:
    """Extrait une vraie URL texte depuis le champ photo, quel que soit
    son type réel (chaîne directe, objet avec attributs, ou dict)."""
    if photo_obj is None:
        return None
    if isinstance(photo_obj, str):
        return photo_obj if photo_obj.startswith("http") else None
    if isinstance(photo_obj, dict):
        for key in ("url", "full_size_url", "thumbnail_url", "dominant_color_full_size_url"):
            value = photo_obj.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
        return None
    # Objet avec attributs (cas le plus courant selon la version de la lib)
    for attr in ("url", "full_size_url", "thumbnail_url"):
        value = getattr(photo_obj, attr, None)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return None


class VintedSource:
    name = "vinted"

    def __init__(self, searches: Optional[list] = None):
        self.scraper = VintedScraper(VINTED_BASE_URL)
        self.searches = searches or CATEGORIES[_current_category]

    def _build_params(self, search: dict) -> dict:
        params = {
            "search_text": search["query"],
            "order": "newest_first",
            "currency": "EUR",
        }
        price_max = search.get("price_max") or VINTED_PRICE_MAX
        if price_max:
            params["price_to"] = price_max
        return params

    def fetch(self) -> list[dict]:
        global _last_stats
        stats = ScanStats()
        all_listings: list[dict] = []

        for search in self.searches:
            params = self._build_params(search)
            logger.info("Recherche Vinted [%s]: %s", _current_category, params)

            try:
                items = self.scraper.search(params)
            except Exception:
                logger.exception("Échec de la recherche Vinted pour %s", search)
                stats.errors += 1
                continue

            for item in items[:VINTED_RESULTS_PER_SEARCH]:
                stats.found_raw += 1
                try:
                    listing = self._format(item, search)
                except Exception:
                    logger.exception(
                        "Échec traitement d'une annonce Vinted (recherche %s)",
                        search.get("query", "?"),
                    )
                    stats.errors += 1
                    continue
                if listing:
                    stats.accepted_source += 1
                    all_listings.append(listing)

        _last_stats = stats
        logger.info("Scan source Vinted terminé [%s]: %s", _current_category, stats.as_dict())
        return all_listings

    def _format(self, item, search: dict) -> Optional[dict]:
        title = getattr(item, "title", None)
        url = getattr(item, "url", None)
        if not title or not url:
            return None

        photo_url = _extract_photo_url(getattr(item, "photo", None))

        vinted_status = getattr(item, "status", None)
        size_title = getattr(item, "size_title", None)

        seller_login = None
        user_obj = getattr(item, "user", None)
        if user_obj is not None:
            seller_login = getattr(user_obj, "login", None)
            if seller_login is None and isinstance(user_obj, dict):
                seller_login = user_obj.get("login")

        return {
            "source": self.name,
            "source_search_query": search.get("query", ""),
            "title": title,
            "description": "",
            "price_raw": getattr(item, "price", None),
            "currency": "EUR",
            "location": "FR",
            "url": url,
            "photos": [photo_url] if photo_url else [],
            "posted_at": None,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "vinted_status": vinted_status,
            "vinted_size": size_title,
            "seller_login": seller_login,
        }


def fetch() -> list[dict]:
    source = VintedSource()
    return source.fetch()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    results = fetch()
    for r in results[:5]:
        print(r)
    print("Stats:", get_last_stats())
