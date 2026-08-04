# Bot Vinted iPhone (France, EUR)

Bot séparé de `iphone-deals-bot` — scan Vinted uniquement, calculateur de
rentabilité en EUR (Vinted ne facture aucun frais de vente, contrairement
à Facebook Marketplace).

## Mise en place

1. **Nouveau repo GitHub** : crée un repo vide et ajoute tous les fichiers
   de cette structure (mêmes dossiers/noms).

2. **Nouveau bot Telegram** : via @BotFather sur Telegram, crée un nouveau
   bot (`/newbot`), récupère le token. Crée un nouveau chat/groupe pour les
   notifications, récupère son chat_id.

3. **Nouveau projet Railway** :
   - Crée un nouveau projet, connecte ce repo GitHub.
   - Ajoute un plugin Postgres (Railway en génère un automatiquement,
     `DATABASE_URL` sera injecté tout seul).
   - Variables d'environnement à ajouter :
     - `TELEGRAM_BOT_TOKEN` = token du nouveau bot
     - `TELEGRAM_CHAT_ID` = chat_id du nouveau chat
     - `CYCLE_SECONDS` = 900 (ou autre, en secondes)
     - `SCORE_THRESHOLD` = 55 (ou autre, sur 100)
     - `VINTED_PRICE_MAX` = optionnel, ex: 150 (prix max en EUR)
     - `ANTHROPIC_API_KEY` = optionnel, seulement si tu actives
       `USE_ANTHROPIC_NORMALIZER=true`

4. **Initialiser la base** : une fois le service déployé, exécute le
   contenu de `storage/schema.sql` sur ta base Postgres (onglet Postgres
   de Railway → Query, ou via `psql` si tu as un accès).

5. Le bot démarre, répond à `/start`, `/menu`, et lance un scan Vinted
   automatique toutes les `CYCLE_SECONDS` secondes.

## Structure

```
main.py                    — bot Telegram (calculateur + scan + menu)
sources/vinted.py           — scraping Vinted (lib vinted-scraper)
pipeline/
  relevance.py              — filtre accessoires/pièces/services/etc.
  normalize.py               — extraction modèle/état/batterie
  estimate.py                — calcul marge/ROI estimés (EUR)
  score.py                    — score sur 100
  vocab.py                    — liste des modèles/états connus
storage/
  db.py                       — connexion Postgres + insertion
  schema.sql                  — schéma de la base (à exécuter une fois)
requirements.txt
```

