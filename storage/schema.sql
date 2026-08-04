-- storage/schema.sql — bot Vinted (France, EUR)
--
-- Schéma Postgres pour :
-- 1. listings          — annonces Vinted brutes + normalisées
-- 2. operations        — tes achats/réparations/reventes réels (apprentissage)
-- 3. model_price_stats — vue matérialisée : prix de revente moyen glissant par
--    modèle+état, calculée à partir de tes opérations réelles
--
-- Convention : tous les montants en EUR (marché Vinted France uniquement,
-- contrairement à iphone-deals-bot qui gère CAD pour Facebook Marketplace
-- Montréal — les deux bots ont des bases séparées, donc pas de conflit).

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================
-- 1. ANNONCES
-- ============================================================
CREATE TABLE IF NOT EXISTS listings (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source              TEXT NOT NULL,
    source_url          TEXT NOT NULL UNIQUE,
    fingerprint         TEXT,

    title               TEXT NOT NULL,
    description         TEXT,
    price_raw           TEXT,
    price_eur           NUMERIC(10, 2),
    location            TEXT,
    photos              JSONB DEFAULT '[]',

    model               TEXT,
    condition           TEXT,
    battery_health      INTEGER,
    storage_gb          INTEGER,
    extraction_confidence TEXT,

    estimated_resale_eur NUMERIC(10, 2),
    estimated_repair_eur NUMERIC(10, 2),
    estimated_margin_eur NUMERIC(10, 2),
    estimated_roi_pct    NUMERIC(6, 2),
    score                NUMERIC(5, 2),

    feedback            TEXT,
    notified            BOOLEAN NOT NULL DEFAULT FALSE,
    notified_at         TIMESTAMPTZ,

    posted_at           TIMESTAMPTZ,
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_listings_model_condition ON listings (model, condition);
CREATE INDEX IF NOT EXISTS idx_listings_score ON listings (score DESC);
CREATE INDEX IF NOT EXISTS idx_listings_fetched_at ON listings (fetched_at DESC);

-- ============================================================
-- 2. OPÉRATIONS RÉELLES (achat -> réparation -> revente)
-- ============================================================
CREATE TABLE IF NOT EXISTS operations (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    listing_id          UUID REFERENCES listings (id),

    model               TEXT NOT NULL,
    condition_at_purchase TEXT NOT NULL,

    purchase_price_eur  NUMERIC(10, 2) NOT NULL,
    purchase_date       DATE NOT NULL,

    repair_cost_eur     NUMERIC(10, 2) DEFAULT 0,
    repair_notes        TEXT,

    resale_price_eur    NUMERIC(10, 2),
    resale_date         DATE,
    resale_channel      TEXT,

    status              TEXT NOT NULL DEFAULT 'in_stock',

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_operations_model ON operations (model);
CREATE INDEX IF NOT EXISTS idx_operations_status ON operations (status);

CREATE OR REPLACE VIEW operations_with_profit AS
SELECT
    *,
    (resale_price_eur - purchase_price_eur - repair_cost_eur) AS net_profit_eur,
    CASE
        WHEN (purchase_price_eur + repair_cost_eur) > 0
        THEN ROUND(
            100.0 * (resale_price_eur - purchase_price_eur - repair_cost_eur)
            / (purchase_price_eur + repair_cost_eur),
            2
        )
        ELSE NULL
    END AS roi_pct,
    (resale_date - purchase_date) AS days_to_resale
FROM operations
WHERE resale_price_eur IS NOT NULL;

-- ============================================================
-- 3. ESTIMATION GLISSANTE PAR MODÈLE + ÉTAT
-- ============================================================
CREATE MATERIALIZED VIEW IF NOT EXISTS model_price_stats AS
SELECT
    model,
    condition_at_purchase AS condition,
    COUNT(*)                              AS sample_size,
    ROUND(AVG(resale_price_eur), 2)       AS avg_resale_eur,
    ROUND(AVG(repair_cost_eur), 2)        AS avg_repair_eur,
    ROUND(AVG(net_profit_eur), 2)         AS avg_net_profit_eur,
    ROUND(AVG(roi_pct), 2)                AS avg_roi_pct,
    ROUND(AVG(days_to_resale), 1)         AS avg_days_to_resale,
    MAX(resale_date)                      AS last_sale_date
FROM operations_with_profit
GROUP BY model, condition_at_purchase;

CREATE UNIQUE INDEX IF NOT EXISTS idx_model_price_stats_key
    ON model_price_stats (model, condition);

-- À rafraîchir après chaque nouvelle vente enregistrée :
-- REFRESH MATERIALIZED VIEW CONCURRENTLY model_price_stats;
