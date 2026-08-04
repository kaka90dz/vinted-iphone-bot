"""
sources/vinted.py

Récupère les annonces Vinted (France) via la lib `vinted-scraper`
(maintenue activement, contrairement à `pyVinted` qui est abandonné
depuis 2022 et incompatible avec Python 3.13).

Interface commune attendue par le pipeline : fetch() -> list[dict]

Différences à garder en tête :
- Pas de coordonnées GPS précises par annonce -> pas de filtre
  géographique ; Vinted FR est un marché national.
- La recherche catalogue ne renvoie pas de date de publication -> on
  laisse posted_at à None systématiquement.
- Une seule photo (item.photo) exposée par la recherche catalogue.
- La description complète n'est pas dans les résultats de recherche
  (seulement dans la page détail de l'annonce) ; on la laisse vide ici
  pour rester rapide.
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

DEFAULT_SEARCHES = [
    {"query": "iphone cassé"},
    {"query": "iphone écran fissuré"},
    {"query": "iphone hs"},
    {"query": "iphone pour pièces"},
    {"query": "iphone ne s'allume plus"},
    {"query": "iphone icloud"},
]


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


class VintedSource:
    name = "vinted"

    def __init__(self, searches: Optional[list] = None):
        self.scraper = VintedScraper(VINTED_BASE_URL)
        self.searches = searches or DEFAULT_SEARCHES

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
            logger.info("Recherche Vinted: %s", params)

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
        logger.info("Scan source Vinted terminé: %s", stats.as_dict())
        return all_listings

    def _format(self, item, search: dict) -> Optional[dict]:
        title = getattr(item, "title", None)
        url = getattr(item, "url", None)
        if not title or not url:
            return None

        photo = getattr(item, "photo", None)

        return {
            "source": self.name,
            "source_search_query": search.get("query", ""),
            "title": title,
            "description": "",
            "price_raw": getattr(item, "price", None),
            "currency": "EUR",
            "location": "FR",
            "url": url,
            "photos": [photo] if photo else [],
            "posted_at": None,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
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
