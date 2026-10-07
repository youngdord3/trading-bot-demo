# Trading Bot Demo (Binance Futures Testnet)

Progetto modulare per un Trading Core sicuro, robusto e isolato, progettato con principi di **defense-in-depth** e minimi privilegi. Attualmente operativo in modalità **Paper Trading** con verifica automatica dei rischi, circuit breaker e persistenza append-only.

---

## 🏛️ Architettura dei Servizi

Il sistema è suddiviso in 4 servizi Docker indipendenti, separati su due reti virtuali:

```text
┌────────────────────────────────────────────────────────────────────────┐
│                              DOCKER NETWORKS                           │
│                                                                        │
│   ┌────────────────┐       backend (internal)       ┌──────────────┐   │
│   │   reconciler   ├───────────────────────────────►│   postgres   │   │
│   │  (audit/latch) │                                │    :5432     │   │
│   └────────────────┘                                └──────▲──▲────┘   │
│                                                            │  │        │
│   ┌────────────────┐       backend (internal)              │  │        │
│   │      core      ├───────────────────────────────────────┘  │        │
│   │  (read-only)   ├──────────────────────────┐               │        │
│   └────────────────┘      exchange_egress     │               │        │
│                                               │               │        │
│   ┌────────────────┐       backend (internal) │               │        │
│   │  paper-trader  ├──────────────────────────┼───────────────┘        │
│   │ (simulazione)  ├────────────┐             │                        │
│   └────────────────┘            │             │                        │
│                          exchange_egress      │                        │
│                                 ▼             ▼                        │
│                    ┌──────────────────────────────────────┐            │
│                    │  Binance Futures Demo (testnet)      │            │
│                    │  demo-fapi.binance.com               │            │
│                    └──────────────────────────────────────┘            │
└────────────────────────────────────────────────────────────────────────┘
```

1. **`postgres` (PostgreSQL 17)**:
   - Residente sulla rete `backend` isolata (`internal: true`), senza porte mappate sull'host.
   - Tutti i vincoli di sicurezza (`orders_enabled = false`, stati consentiti `PAUSED`/`HALTED`) sono applicati a livello di constraint SQL fisici.
2. **`core` (Python 3.13-slim)**:
   - Server HTTP in sola lettura (`/health`, `/ready` protetto da Bearer token).
   - Utilizza il ruolo DB a minimi privilegi `core_reader`.
   - Accesso alla rete `exchange_egress` solo per le verifiche pubbliche e private read-only.
3. **`reconciler`**:
   - Processo periodico/on-demand su rete `backend`.
   - Utilizza il ruolo `core_reconciler` per verificare lo stato e invocare funzioni `SECURITY DEFINER` (`bot_breaker`) in caso di anomalie.
4. **`paper-trader`**:
   - Loop autonomo su timeframe 15m con strategia EMA 9/21.
   - Rete `backend` (per DB) ed `exchange_egress` (per interrogare klines pubbliche su Binance Demo).
   - Utilizza il ruolo dedicato `core_paper_trader`. Nessun segnale viene inviato a Binance; le operazioni simulate vengono scritte atomicamente in `bot.paper_trades`.

---

## 🗄️ Elenco Migrazioni Database

Tutti i cambiamenti strutturali al database sono tracciati in file numerati in `database/` e `migrations/`:

| Versione | File | Descrizione |
|---|---|---|
| **001** | `database/001_bootstrap.sql` | Bootstrap dello schema `bot`, tabella `system_state` con vincolo `orders_enabled = false`, tabella `schema_migrations`, tabella immutabile `audit_events`. |
| **002** | `migrations/002_core_reader.sql` | Ruolo `core_reader` in sola lettura (`default_transaction_read_only = on`) con permessi `SELECT` ristretti a `system_state` e `schema_migrations`. |
| **003** | `migrations/003_core_reconciler.sql` | Tabella `reconciliation_snapshots` e ruolo `core_reconciler` con permessi di inserimento snapshot per le verifiche dell'account. |
| **004** | `migrations/004_circuit_breaker.sql` | Ruolo `bot_breaker` (`NOLOGIN`), funzioni `SECURITY DEFINER` `record_reconciliation_anomaly` e `trip_circuit_breaker` per transizione unidirezionale verso lo stato `HALTED`. |
| **005** | `migrations/005_paper_trading.sql` | Tabelle append-only `bot.paper_processed_candles` (deduplicazione candele a livello DB su chiave primaria `symbol, interval, candle_open_time_ms`) e `bot.paper_trades` (registro esecuzioni simulate); ruolo `core_paper_trader`. |

---

## 🔐 Gestione dei Segreti (Docker Secrets)

Nessun segreto è memorizzato in variabili d'ambiente, codice sorgente o file git. Tutti i file risiedono nella cartella locale non versionata `secrets/`:

| Nome Secret | File Richiesto | Scopo |
|---|---|---|
| `postgres_password` | `secrets/postgres_password.txt` | Password amministrativa di bootstrap PostgreSQL (`bootstrap_admin`) |
| `core_db_password` | `secrets/core_db_password.txt` | Password del ruolo database `core_reader` |
| `core_reconciler_db_password` | `secrets/core_reconciler_db_password.txt` | Password del ruolo database `core_reconciler` |
| `core_paper_trader_db_password` | `secrets/core_paper_trader_db_password.txt` | Password del ruolo database `core_paper_trader` |
| `core_api_token` | `secrets/core_api_token.txt` | Token Bearer per autenticare le richieste a `/ready` |
| `binance_api_key` | `secrets/binance_api_key.txt` | API Key pubblica/read-only per Binance Futures Demo |
| `binance_api_secret` | `secrets/binance_api_secret.txt` | API Secret per Binance Futures Demo |

### Generazione Segreti Casuali (PowerShell)
```powershell
New-Item -ItemType Directory -Force -Path secrets

# Genera stringhe casuali crittograficamente sicure a 64 caratteri
function New-RandomSecret {
    $bytes = New-Object byte[] 48
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes)
}

New-RandomSecret | Out-File -FilePath secrets/postgres_password.txt -NoNewline -Encoding utf8
New-RandomSecret | Out-File -FilePath secrets/core_db_password.txt -NoNewline -Encoding utf8
New-RandomSecret | Out-File -FilePath secrets/core_reconciler_db_password.txt -NoNewline -Encoding utf8
New-RandomSecret | Out-File -FilePath secrets/core_paper_trader_db_password.txt -NoNewline -Encoding utf8
New-RandomSecret | Out-File -FilePath secrets/core_api_token.txt -NoNewline -Encoding utf8
```
*(Per `binance_api_key.txt` e `binance_api_secret.txt`, inserire le chiavi generate dalla dashboard Binance Futures Testnet).*

---

## 💻 Comandi Principali

### 1. Build dell'Immagine
```powershell
docker compose -f compose.yaml -f compose.public.yaml build core
```

### 2. Test Unitari (completamente isolati e senza rete)
```powershell
docker run --rm --network none trading-bot-demo-core python -m unittest discover -s tests -v
```

### 3. Backtest Offline
Il backtest interroga `fapi.binance.com` per dati storici reali e calcola PnL, taker fee, slippage e drawdown:
```powershell
# Default: 90 giorni su BTCUSDT
docker compose -f compose.yaml -f compose.public.yaml exec -T core python backtest.py

# Finestra a 180 giorni
docker compose -f compose.yaml -f compose.public.yaml exec -T -e BACKTEST_DAYS=180 core python backtest.py
```

### 4. Avvio e Arresto Paper Trader
```powershell
# Avvio in background
docker compose -f compose.yaml -f compose.public.yaml -f compose.paper.yaml up -d paper-trader

# Verifica log in tempo reale
docker compose -f compose.yaml -f compose.public.yaml -f compose.paper.yaml logs -f paper-trader

# Arresto pulito
docker compose -f compose.yaml -f compose.public.yaml -f compose.paper.yaml stop paper-trader
```

### 5. Report di Coerenza Paper Trading
Verifica l'alternanza corretta delle posizioni aperte/chiuse, l'integrità dei trade rispetto alle candele elaborate e il PnL simulato:
```powershell
docker compose -f compose.yaml -f compose.public.yaml -f compose.paper.yaml exec -T paper-trader python paper_report.py
```

---

## 📖 Mini Runbook Operativo

### 🔴 Scenario 1: Errore `ANOTHER_PAPER_TRADER_RUNNING`
- **Sintomo**: Il processo termina all'avvio con codice `2` e log `{"event": "PAPER_TRADER_FATAL", "error": "ANOTHER_PAPER_TRADER_RUNNING"}`.
- **Causa**: PostgreSQL rifiuta il lock advisory applicativo (`pg_try_advisory_lock(hashtext('paper_trader'))`). Un'altra istanza è già attiva o una connessione precedente è rimasta aperta.
- **Risoluzione**:
  1. Controlla i container attivi: `docker compose -f compose.yaml -f compose.public.yaml -f compose.paper.yaml ps`.
  2. Se necessario, ferma eventuali container duplicati: `docker compose -f compose.yaml -f compose.public.yaml -f compose.paper.yaml stop paper-trader`.
  3. Per terminare connessioni orfane sul DB:
     ```sql
     SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = 'paper_trader' AND pid <> pg_backend_pid();
     ```
  4. Riavvia il servizio: `docker compose -f compose.yaml -f compose.public.yaml -f compose.paper.yaml up -d paper-trader`.

### 🟡 Scenario 2: Arresto con `PAPER_TRADER_ABORT`
- **Sintomo**: Il trader si arresta con codice `1` ed evento `PAPER_TRADER_ABORT` con `consecutive_failures=10`.
- **Causa**: Più di 10 errori consecutivi di connessione (rete esterna non raggiungibile, timeout Binance o Postgres non disponibile).
- **Risoluzione**:
  1. Verifica lo stato di salute di PostgreSQL: `docker compose exec postgres pg_isready -U bootstrap_admin -d trading_demo`.
  2. Verifica l'uscita su Internet eseguendo il check pubblico: `docker compose -f compose.yaml -f compose.public.yaml exec core python check_binance_public.py`.
  3. Controlla i log precedenti per individuare l'eccezione esatta: `docker compose -f compose.yaml -f compose.public.yaml -f compose.paper.yaml logs --tail 50 paper-trader`.
  4. Una volta ristabilita la connettività, riavvia: `docker compose -f compose.yaml -f compose.public.yaml -f compose.paper.yaml up -d paper-trader`.

### 🚨 Scenario 3: Stato di Sistema `HALTED` (Circuit Breaker Tripped)
- **Sintomo**: I log del trader riportano continuativamente `PAPER_HALTED_SKIP`, e nessuna operazione o candela viene registrata.
- **Causa**: È scattato il circuit breaker di sicurezza a seguito di un'anomalia rilevata dal `reconciler` o di un blocco manuale.
- **Risoluzione**:
  1. Identifica l'evento e la causa scatenante negli audit log:
     ```powershell
     docker compose exec -T postgres psql -U bootstrap_admin -d trading_demo -c "SELECT event_type, details, created_at FROM bot.audit_events ORDER BY id DESC LIMIT 5;"
     ```
  2. Verifica che le posizioni e il margine sull'account Binance siano regolari (`check_binance_account.py`).
  3. Solo dopo aver risolto e validato l'anomalia, l'amministratore può ripristinare manualmente lo stato a `PAUSED`:
     ```powershell
     docker compose exec -T postgres psql -U bootstrap_admin -d trading_demo -c "UPDATE bot.system_state SET status = 'PAUSED', orders_enabled = false, reason = 'Ripristino amministrativo post-anomalia', updated_at = now() WHERE id = 1;"
     ```
  4. Il `paper-trader` rileverà il cambio di stato al ciclo successivo (entro 30s) e riprenderà la regolare attività.
