"""
storage/db.py — bot Vinted

Connexion Postgres partagée. Railway injecte automatiquement DATABASE_URL
quand tu ajoutes un plugin Postgres à ton projet.

Base séparée de celle de iphone-deals-bot (nouveau projet Railway) — donc
aucun risque d'interférence entre les deux bots.

Encodage : client_encoding forcé à UTF8 explicitement (même correctif que
sur iphone-deals-bot, pour éviter tout encodage corrompu dans les logs).
"""

import os
import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL")


def get_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL manquant (ajoute un plugin Postgres sur Railway).")
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.set_client_encoding("UTF8")
    return conn


def insert_listing(listing: dict) -> str:
    """Insère une annonce (normalisée + estimée + scorée). Ignore les
    doublons d'URL — source_url reste la clé logique anti-doublon."""
    n = listing.get("normalized", {})
    e = listing.get("estimation", {})
    r = listing.get("relevance", {})

    query = """
        INSERT INTO listings (
            source, source_url, title, description, price_raw, price_eur,
            location, photos, model, condition, battery_health, storage_gb,
            extraction_confidence, estimated_resale_eur, estimated_repair_eur,
            estimated_margin_eur, estimated_roi_pct, score, posted_at,
            listing_type, rejection_reason
        ) VALUES (
            %(source)s, %(source_url)s, %(title)s, %(description)s, %(price_raw)s, %(price_eur)s,
            %(location)s, %(photos)s, %(model)s, %(condition)s, %(battery_health)s, %(storage_gb)s,
            %(extraction_confidence)s, %(estimated_resale_eur)s, %(estimated_repair_eur)s,
            %(estimated_margin_eur)s, %(estimated_roi_pct)s, %(score)s, %(posted_at)s,
            %(listing_type)s, %(rejection_reason)s
        )
        ON CONFLICT (source_url) DO NOTHING
        RETURNING id;
    """
    params = {
        "source": listing.get("source"),
        "source_url": listing.get("url"),
        "title": listing.get("title"),
        "description": listing.get("description"),
        "price_raw": str(listing.get("price_raw")),
        "price_eur": e.get("listing_price_eur"),
        "location": listing.get("location"),
        "photos": psycopg2.extras.Json(listing.get("photos", [])),
        "model": n.get("model"),
        "condition": n.get("condition"),
        "battery_health": n.get("battery_health"),
        "storage_gb": n.get("storage_gb"),
        "extraction_confidence": n.get("confidence"),
        "estimated_resale_eur": e.get("estimated_resale_eur"),
        "estimated_repair_eur": e.get("estimated_repair_eur"),
        "estimated_margin_eur": e.get("margin_eur"),
        "estimated_roi_pct": e.get("roi_pct"),
        "score": listing.get("score"),
        "posted_at": listing.get("posted_at"),
        "listing_type": r.get("listing_type"),
        "rejection_reason": r.get("rejection_reason"),
    }

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            conn.commit()
            return row["id"] if row else None


def mark_notified(listing_id: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE listings SET notified = TRUE, notified_at = now() WHERE id = %s",
                (listing_id,),
            )
            conn.commit()


def get_recent_sent(limit: int = 10) -> list:
    """Utilisé par le bouton Telegram 'Historique'."""
    query = """
        SELECT title, model, condition, score, estimated_margin_eur, source_url, notified_at
        FROM listings
        WHERE notified = TRUE
        ORDER BY notified_at DESC
        LIMIT %s;
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (limit,))
            return cur.fetchall()
