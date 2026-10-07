# Documento di Handoff — Fase Paper Trading (v0.5-paper-trading)

Questo documento sintetizza lo stato del progetto, l'architettura collaudata, la cronologia dei commit (Passi 0–10), i risultati quantitativi del backtest, i controlli di integrità eseguiti e i limiti operativi noti.

---

## 1. Stato della Fase e Cronologia Commit

La fase di implementazione del **Paper Trading e Risk Management** è completata, testata end-to-end e isolata a livello infrastrutturale.

| Passo | Hash Commit | Tipo/Scope | Descrizione |
|---|---|---|---|
| **0** | `59d4cc8` | `chore(repo)` | Allineamento Dockerfile, secrets e file delle fasi 1-10 |
| **1** | `c5dc0d6` | `feat(core)` | Modulo common con log JSON, lettura secrets, db_connect autocommit e backoff |
| **2** | `956661c` | `feat(core)` | Client pubblico con whitelist, misurazione time offset e supporto MARKET_LOT_SIZE |
| **3** | `2170798` | `feat(strategy)` | Signal engine EMA 9/21 crossover su sole candele chiuse e verifica continuità |
| **4** | `f8ad25c` | `feat(risk)` | Modello a azioni (OPEN/CLOSE), sizing dinamico da min_notional, 12 test di regressione |
| **5** | `67aea18` | `feat(backtest)` | Backtest offline su mainnet pubblica: klines paginate, taker fee, slippage, fill a next open |
| **6** | `37b8484` | `feat(db)` | Migrazione 005 paper trading append-only con dedup a livello DB e ruolo `core_paper_trader` |
| **7** | `5a2f1e7` | `feat(paper)` | Loop paper trader con lock advisory, transazione atomica candela+operazioni e HALTED check |
| **8** | `143771e` | `feat(paper)` | Servizio containerizzato `paper-trader` isolato con secret dedicato e hardening Linux |
| **9** | `bd1dbb4` | `feat(paper)` | Report di coerenza `paper_report.py` e collaudo (riavvio, istanza singola, HALTED latch) |
| **10** | *(questo commit)* | `docs` | README completo, documentazione di handoff e runbook operativo |

---

## 2. Risultati del Backtest Offline (Strategia EMA 9/21)

I test sono stati eseguiti scaricando i dati reali da `fapi.binance.com` senza look-ahead bias (esecuzione simulata al prezzo open della candela successiva, taker fee 0.05% per lato, slippage sfavorevole di 2 bps).

| Metrica | Finestra 90 Giorni | Finestra 180 Giorni |
|---|---|---|
| **Candele 15m analizzate** | 8.639 | 17.279 |
| **Buchi nei dati (gap)** | 0 | 0 |
| **Operazioni totali (round trips)** | 405 | 803 |
| **Operazioni vincenti / perdenti** | 90 / 315 | 180 / 623 |
| **Win Rate** | **22.2%** | **22.4%** |
| **Profit Factor** | **0.76** | **0.64** |
| **PnL Netto (USDT)** | **-71.13 USDT** | **-199.16 USDT** |
| **Commissioni pagate (USDT)** | 59.17 USDT | 115.33 USDT |
| **Max Drawdown (USDT)** | 78.78 USDT | 208.86 USDT |
| **Benchmark Buy & Hold (USDT)** | +38.98 USDT | +24.47 USDT |

> [!WARNING]
> La strategia pura di incrocio EMA 9/21 su timeframe 15m soffre di continuo whipsaw nei periodi di mercato laterale e l'impatto cumulativo delle commissioni erode significativamente il capitale. Il paper trading serve a validare **l'affidabilità dell'infrastruttura, della deduplica e dei vincoli di rischio**, non la redditività economica di questo specifico segnale.

---

## 3. Esito del Collaudo Operativo (Passo 9)

1. **Deduplicazione e Riavvio Brutale**:
   - `paper-trader` è stato terminato con `kill` e riavviato.
   - Alla chiusura della candela successiva, il contatore `candles_processed` è incrementato di esattamente 1 unità (+1), attestando che la deduplicazione su chiave primaria `(symbol, interval, candle_open_time_ms)` impedisce qualsiasi doppia elaborazione dopo riavvii o crash.
2. **Enforcement Istanza Singola (Advisory Lock)**:
   - Il tentativo di avviare un secondo processo concorrente su PostgreSQL termina immediatamente con codice di uscita `2` ed evento JSON `{"event": "PAPER_TRADER_FATAL", "error": "ANOTHER_PAPER_TRADER_RUNNING"}`.
3. **Circuit Breaker e Stato HALTED**:
   - Scatto attivato via `bot.trip_circuit_breaker('MANUALTEST')` come utente `bootstrap_admin`.
   - Il `paper-trader` ha rilevato `status == 'HALTED'` entro 10s, sospendendo qualsiasi elaborazione e invio di ordini e stampando a log `PAPER_HALTED_SKIP` ogni 30s.
   - Verificato nel DB che nessuna nuova riga è stata inserita durante lo stato di stop.
   - Ripristinato lo stato a `PAUSED` da amministratore, il bot ha ripreso automaticamente la scansione e sincronizzazione oraria.
4. **Isolamento del Reconciler**:
   - Il servizio `reconciler` continua ad eseguire regolarmente i controlli preflight con esito `passed: true`, confermando che l'ambiente rimane isolato e protetto.

---

## 4. Limiti Noti del Paper Trading

- **Modello di Fill**: Il prezzo simulato corrisponde all'ultimo prezzo ticker di mercato sommato a uno slippage fisso sfavorevole (`0.0002`), senza interrogare la profondità dell'order book (`depth`).
- **Assenza di Funding Rate**: Nei contratti perpetual Binance Futures, il pagamento del funding ogni 8 ore non viene addebitato/accreditato nel calcolo del PnL simulato.
- **Fill Completi (No Parziali)**: Ogni ordine simulato viene eseguito istantaneamente per l'intero volume calcolato.
- **Timeframe e Strumento Fissi**: Configurato esclusivamente per `BTCUSDT` su intervallo `15m`.
- **Nessun Recupero Storico (Backfill)**: Se il container del paper trader rimane spento durante la chiusura di più candele, al riavvio riprende dalla sola candela più recente senza simulare trade retroattivi per le candele perse.
- **Invariante di Blocco**: Il constraint del DB `bot.system_state` impone `orders_enabled = false`. Nessuna transazione reale viene inviata all'exchange.
