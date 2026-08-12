"""
pipeline/vinted_buy.py

Tentative d'achat sur Vinted via l'API interne, en utilisant le token de
session OAuth du compte utilisateur (récupéré manuellement dans les
outils développeur du navigateur — voir /aide sur le bot).

IMPORTANT — honnêteté technique :
Vinted ne documente aucune API d'achat publique. L'endpoint et le format
exact du payload de checkout ci-dessous sont une meilleure estimation
basée sur la structure REST habituelle de Vinted, mais n'ont PAS été
vérifiés par un achat réel réussi. Il est très possible que ça échoue
au premier essai — dans ce cas, le bot bascule automatiquement sur un
bouton "🛒 Acheter maintenant" manuel en secours (voir main.py), donc
aucune affaire n'est jamais ratée silencieusement.

Pour fiabiliser l'endpoint toi-même :
  1. Connecte-toi sur vinted.fr dans Chrome/Firefox
  2. Ouvre les outils développeur > onglet Réseau, coche "Persist logs"
  3. Fais un achat normal (petit montant, test) et clique "Acheter"
  4. Repère la requête POST qui déclenche réellement le paiement
     (cherche un nom du type "checkout", "transactions", "orders")
  5. Note son URL exacte, ses headers, et le corps JSON envoyé
  6. Remplace BUY_ENDPOINT et _build_payload() ci-dessous avec ces valeurs
     exactes (demande-moi de l'aide pour adapter le code une fois que tu
     as ces infos, ce sera rapide)

Pour récupérer ton token (à donner ensuite au bot via /settoken) :
  1. Connecte-toi sur vinted.fr
  2. Outils développeur > Réseau > coche "Persist logs"
  3. Recharge la page, tape "oauth" dans la barre de recherche du panneau
  4. Clique sur la requête "oauth" trouvée, va dans l'onglet Réponse
  5. Copie la valeur "access_token"
"""

import re
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

VINTED_API_BASE = "https://www.vinted.fr"

# À AJUSTER une fois l'endpoint réel capturé — voir docstring ci-dessus.
# Ceci est une estimation non vérifiée.
BUY_ENDPOINT = "/api/v2/transactions/init"

_ITEM_ID_RE = re.compile(r"/items/(\d+)")


class AutobuyError(Exception):
    """Levée quand la tentative d'achat automatique échoue.
    L'appelant doit alors proposer le bouton d'achat manuel en secours."""


def extract_item_id(url: str) -> Optional[str]:
    """Extrait l'ID numérique Vinted d'une URL d'annonce, ex:
    https://www.vinted.fr/items/1234567890-mon-iphone -> '1234567890'"""
    match = _ITEM_ID_RE.search(url)
    return match.group(1) if match else None


def _build_headers(token: str, csrf_token: Optional[str] = None) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
        ),
    }
    if csrf_token:
        headers["X-CSRF-Token"] = csrf_token
    return headers


def _build_payload(item_id: str) -> dict:
    # À AJUSTER selon la capture réseau réelle (voir docstring du module).
    return {"item_id": item_id}


async def attempt_purchase(token: str, item_id: str, csrf_token: Optional[str] = None) -> dict:
    """Tente un achat automatique sur Vinted.

    Lève AutobuyError en cas d'échec (mauvais token, endpoint incorrect,
    item déjà vendu, etc.) — l'appelant doit alors proposer le bouton
    d'achat manuel en secours plutôt que de considérer l'annonce perdue.
    """
    headers = _build_headers(token, csrf_token)
    payload = _build_payload(item_id)
    url = f"{VINTED_API_BASE}{BUY_ENDPOINT}"

    logger.info("Tentative d'achat automatique pour l'item %s", item_id)

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise AutobuyError(f"Erreur réseau: {exc}") from exc

    if response.status_code >= 400:
        raise AutobuyError(
            f"Vinted a refusé la requête d'achat (HTTP {response.status_code}): "
            f"{response.text[:300]}"
        )

    try:
        return response.json()
    except ValueError:
        return {"raw": response.text[:300]}
