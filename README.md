# Trading Bot Demo

Progetto modulare per un Trading Core sicuro, isolato e controllato, progettato con principi di *defense-in-depth*.

## 🛡️ Architettura di Sicurezza

- **PostgreSQL 17**: Servizio database isolato su rete Docker interna (`backend: internal: true`), senza porte pubblicate sull'host o su Internet.
- **Ruolo Database a Minimi Privilegi (`core_reader`)**: Utente applicativo vincolato alla sola lettura (`default_transaction_read_only = on`), con permessi `SELECT` ristretti esclusivamente a `bot.system_state` e `bot.schema_migrations`.
- **Trading Core (Python 3.13-slim)**:
  - Processo non-root (utente `app`, UID 10001).
  - Filesystem del container in sola lettura (`read_only: true`).
  - Capabilities Linux azzerate (`cap_drop: ALL`, `no-new-privileges: true`).
  - Rete controllata: collegato a `backend` per il DB e a `exchange_egress` per le chiamate in uscita verso Binance Demo.
  - Endpoint `/ready` protetto da token Bearer con confronto a tempo costante (`hmac.compare_digest`).
  - **Invarianti di blocco**: `orders_enabled: false`, `trading_ready: false`, nessun connettore ordini.

---

## 📁 Struttura del Progetto

```text
trading-bot-demo/
  ├── compose.yaml                # Definizione dei servizi PostgreSQL e Core
  ├── compose.public.yaml         # Integrazione rete in uscita per test pubblici
  ├── .gitignore                  # Esclusione automatica di segreti e file sensibili
  ├── README.md                   # Documentazione del progetto
  ├── secrets/                    # [NON VERSIONATO] Cartella dei segreti
  │   ├── postgres_password.txt   # Password amministrativa di bootstrap
  │   ├── core_db_password.txt    # Password utente core_reader
  │   └── core_api_token.txt      # Bearer token per endpoint Core
  ├── database/
  │   └── 001_bootstrap.sql       # Schema bot iniziale, tabelle e audit log
  ├── migrations/
  │   └── 002_core_reader.sql     # Creazione e permessi del ruolo core_reader
  └── core/
      ├── Dockerfile              # Immagine sicura non-root
      ├── requirements.txt        # Dipendenze Python (psycopg)
      ├── app.py                  # Servizio HTTP Trading Core
      └── check_binance_public.py # Client di verifica rete, clock e filtri BTCUSDT
```

---

## 🚀 Setup e Avvio Rapido

### 1. Preparazione dei Segreti
Creare la cartella `secrets/` e inserire tre file di testo contenenti ciascuno una password casuale (almeno 32 caratteri, singola riga, senza virgolette):
- `secrets/postgres_password.txt`
- `secrets/core_db_password.txt`
- `secrets/core_api_token.txt`

### 2. Avvio e Migrazione Database
```powershell
# Avvio di PostgreSQL
docker compose up -d postgres

# Applicazione della migrazione per il ruolo core_reader
docker compose exec -T postgres sh -c 'export CORE_DB_PASSWORD=$(cat /run/secrets/core_db_password); exec psql -U bootstrap_admin -d trading_demo -v ON_ERROR_STOP=1 -f /migrations/002_core_reader.sql'
```

### 3. Build e Avvio del Trading Core
```powershell
docker compose -f compose.yaml -f compose.public.yaml build core
docker compose -f compose.yaml -f compose.public.yaml up -d core
docker compose -f compose.yaml -f compose.public.yaml ps
```

### 4. Verifiche di Integrità
```powershell
# Verifica autenticata stato e invarianti (/ready)
docker compose -f compose.yaml -f compose.public.yaml exec -T core python -c "import json,pathlib,urllib.request; token=pathlib.Path('/run/secrets/core_api_token').read_text().strip(); req=urllib.request.Request('http://127.0.0.1:8000/ready',headers={'Authorization':'Bearer '+token}); print(json.dumps(json.load(urllib.request.urlopen(req,timeout=15)),indent=2))"

# Test pubblico Binance Futures Demo (latenza, clock e filtri di mercato)
docker compose -f compose.yaml -f compose.public.yaml exec -T core python check_binance_public.py
```
