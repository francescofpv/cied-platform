# Piattaforma di integrazione dati CIED

Acquisizione degli export BIOTRONIK Home Monitoring (IEEE 11073-10103),
normalizzazione su PostgreSQL, storia clinica degli impianti ed esportazione CSV.

## Avvio (Windows, PostgreSQL installato)
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env             # poi compila DB_PASSWORD e PSEUDO_KEY
python -c "import secrets; print(secrets.token_hex(32))"   # -> valore per PSEUDO_KEY
python scripts/init_db.py          # crea schemi, tabelle, viste, ruoli, cataloghi
python scripts/load_xml.py "data/samples/*.xml"
pytest -q                          # crea e cancella da solo un DB di test
```
`python scripts/init_db.py --reset` cancella e ricrea tutto (solo in sviluppo).

## Idempotenza: un report entra una volta sola
Una sessione è identificata dalla **chiave naturale** dispositivo (produttore + modello + seriale)
+ data/ora della sessione (`MDC_IDC_SESS_DTM`), protetta dal vincolo `UNIQUE (device_pk, session_dtm)`.
- primo import: `caricato correttamente`;
- stesso report di nuovo, anche in un file diverso: `sessione già presente (duplicato)`; si aggiornano
  solo le notifiche, che possono essere cambiate di stato;
- tutto avviene in una transazione e gli import dello stesso dispositivo si mettono in fila,
  quindi anche due import simultanei producono una sola sessione.

## Esportazione CSV
Il CSV lo scrive PostgreSQL con il suo `COPY ... TO STDOUT (FORMAT csv, HEADER)`; si esportano
solo tabelle e viste dello schema pseudonimizzato `cied` (mai `vault`).
```bash
python scripts/export_csv.py                          # tutte le viste v_* nella cartella export/
python scripts/export_csv.py v_measurement v_implant  # solo quelle indicate
python scripts/export_csv.py --excel                  # separatore ";" e BOM, per Excel in italiano
python scripts/export_csv.py --list                   # cosa si può esportare
python scripts/export_csv.py --gruppi                 # elenca i gruppi di pazienti
python scripts/export_csv.py --gruppo "Studio FA"     # solo i pazienti del gruppo (in export/gruppo_Studio_FA/)
```
Con `--gruppo` ogni vista o tabella viene filtrata attraverso la colonna che porta al paziente
(`patient_pk`, `patient_pseudo`, `device_pk`, `device_pseudo`, `session_pk`, `implant_pk`);
i cataloghi senza pazienti (es. `condition`) si esportano solo senza gruppo.
In Python: `export_csv(conn, "v_measurement", path, group="Studio FA")`.

Nel CSV non escono la data di nascita né l'anno di nascita, ma l'età (`age`, e `age_at_implant`
in `v_implant` e `v_ilr_outcome`, calcolata dall'anno di nascita: ±1 anno); delle date di impianto
esce solo l'anno (`implant_year`, `ilr_implant_year`, `next_implant_year`), anche per le date
d'impianto trasmesse dal dispositivo come valori del report (`MDC_IDC_*IMPLANT_DT`).
Per una query su misura basta `psql`, senza Python:
```
\copy (SELECT * FROM cied.v_measurement WHERE term = 'MDC_IDC_STAT_AT_BURDEN_PERCENT') TO 'burden.csv' WITH (FORMAT csv, HEADER)
```

## Struttura
```
sql/schema.sql                schema (vault + cied), regole cliniche, viste, ruoli
sql/seed.sql                  catalogo patologie (ICD-10-IM) e motivazioni di espianto
src/cied/parser/ieee11073.py  parser XML -> valori appiattiti
src/cied/db/loader.py         import degli export (idempotente con chiave naturale, pseudonimizzato)
src/cied/db/identity.py       pazienti e dispositivi: vault (identità) + cied (pseudonimi)
src/cied/clinical.py          impianti, espianti, patologie
src/cied/pseudo.py            pseudonimi HMAC
src/cied/hm_client.py         client API Home Monitoring (mTLS + Basic Auth, limite richieste, retry)
src/cied/sync.py              download automatico: coda degli export e modalità on demand
src/cied/hm_mock.py           server finto dell'API, per sviluppo e test
src/cied/patients.py          ricerca pazienti, valori per i filtri, gruppi, terapia NAO
src/cied/export.py            export CSV con COPY nativo di PostgreSQL (solo schema cied)
scripts/                      init_db, load_xml, export_csv, sync_hm, mock_hm_server, convert_p12,
                              search_ui (pagina di ricerca), benchmark_search (prova di carico)
tests/                        test su XML sintetici, DB temporaneo e server finto
```

## Modello dei dati
**vault** (identificativo): `patient` (anagrafica), `device_identity` (seriale reale, link HMSC),
`export_raw` (XML originale), `access_log` (accessi ai dati identificativi).

**cied** (pseudonimizzato):
- dati dei dispositivi: `patient`, `device`, `export`, `session`, `observation`, `term`, `notification`
- storia clinica: `implant` (un dispositivo in un paziente, impianto -> espianto),
  `implant_condition` (più patologie per impianto, con ruolo), cataloghi `condition`, `explant_reason`
- viste: `v_measurement`, `v_episode`, `v_zone`, `v_implant`, `v_ilr_outcome`, `v_device_pending`

## Regole cliniche (imposte dal database)
| Momento | Obbligatorio |
|---|---|
| Impianto | data, paziente, ≥1 patologia **PREGRESSA** (`LOCAL:NONE` se nessuna), ≥1 **INDICAZIONE** (motivazione) |
| Espianto (se c'è la data) | motivazione (nota se `OTHER`), ≥1 patologia **DIAGNOSTICATA**, passaggio ad altro CIED sì/no |
| Espianto di loop recorder con passaggio a CIED | se il loop recorder è stato determinante |
| Sempre | CIED successivo dello stesso paziente; niente campi di espianto senza data di espianto |

Facoltativi: data della prima diagnosi (solo per le patologie **DIAGNOSTICATA**), note, collegamento al CIED successivo,
patologie **SOSPETTA** (solo per i loop recorder: il database le rifiuta sugli altri dispositivi).

Patologie diagnosticate in un secondo momento, per qualsiasi dispositivo (anche ancora impiantato,
es. FA non nota rilevata da un pacemaker, o dopo l'espianto):
`add_diagnoses(conn, implant_pk, [Condition("I48", first_diagnosis_date=date(2026, 11, 20))])`.

Patologia sospetta (loop recorder): `register_implant(..., sospette=["I48"])`. All'espianto,
`cied.v_ilr_outcome.sospetto_confermato` vale `true` se almeno una patologia sospetta compare
con lo stesso codice tra le diagnosticate, `false` se nessuna, `NULL` se non c'erano sospetti o il
loop recorder è ancora impiantato. Per aggiornare un database esistente basta `python scripts\init_db.py`.

## Ruoli
- `cied_analyst`: solo schema `cied`.
- `cied_clinician`: inserisce la storia clinica; vede nomi e situazione solo tramite
  `SELECT * FROM vault.clinical_summary();` (ogni chiamata finisce in `vault.access_log`).

## Download automatico (API Home Monitoring)
Riferimento: *EHR DataSync - Web Service Documentation*, Rev. G.

**Con il server finto** (nessuna credenziale necessaria):
```bash
python scripts/mock_hm_server.py                  # terminale 1: serve gli XML di data/samples
python scripts/sync_hm.py --env .env.mock --check # terminale 2: prova la connessione
python scripts/sync_hm.py --env .env.mock         # scarica, importa, segna come processati
python scripts/sync_hm.py --env .env.mock --on-demand
```

**Con l'API reale**, dopo la registrazione dell'export client nel Service Center:
```bash
python scripts/convert_p12.py C:\percorso\XXXXXXXXX-cert.p12   # -> secrets/cert.pem, secrets/private.key
# nel file .env: HM_CLIENT_ID, HM_CLIENT_SECRET (HM_CA_BUNDLE vuoto)
python scripts/sync_hm.py --check
python scripts/sync_hm.py
```
Comportamento: al massimo `HM_MAX_REQ_PER_MIN` richieste al minuto (default 4, la documentazione
indica 5); su 429/500/503 attende e riprova. Un export viene segnato come processato
(`DELETE`) solo dopo che l'import è andato a buon fine; se fallisce resta in coda, ma
BIOTRONIK cancella gli export dopo 14 giorni, quindi la sincronizzazione va eseguita spesso
(es. ogni ora con l'Utilità di pianificazione di Windows).

## Pazienti: codice, ricerca, gruppi, NAO
- **Codice paziente**: numero progressivo automatico (`vault.patient.patient_no`, da 100001), non cambia mai.
  Gli ID esterni (Service Center, altri produttori) sono in `vault.patient_identifier`: se l'ID nel
  Service Center viene modificato, il paziente viene riconosciuto dal dispositivo e il nuovo ID diventa un alias.
- **Dispositivo**: identificato da produttore + modello + seriale (il seriale da solo non è univoco).
- **Terapia NAO**: obbligatoria all'impianto (`register_implant(..., nao_therapy=True/False)`); salvata
  sull'impianto (`nao_at_implant`) e come stato attuale del paziente (`patients.set_nao_therapy`).
- **Gruppi** liberi di pazienti: `patients.create_group`, `add_to_group`, `remove_from_group`.
- **Ricerca**: `vault.search_patients(testo, produttore, modello, gruppo, nao, limite, offset)`; ogni parola
  deve trovare l'inizio di una parola tra cognome, nome, codice, anno di nascita, produttore, modello,
  seriale e ID esterni ("rossi", "d'amico", "Biotronik III", "biomonitor iv 12345678").
  Indice trigram (`pg_trgm`) e normalizzazione di accenti e apostrofi (`unaccent`).

```bash
python scripts/search_ui.py          # pagina di ricerca con tendine: http://127.0.0.1:8000
python scripts/benchmark_search.py   # tempi di ricerca su 50.000 pazienti sintetici (DB temporaneo)
```

## Simulazione e analisi dei loop recorder

Dati **sintetici** per provare la piattaforma e il metodo di analisi (non sono dati reali):

```
python scripts\simula_ilr.py                       300 pazienti con loop recorder (seme 2026, ~1 minuto)
python scripts\analizza_ilr.py export\simulazione  resa diagnostica, tempi, sospetto confermato, batteria, FA
```
`simula_ilr.py` usa un database separato, `cied_simulazione` (cancellato e ricreato a ogni
esecuzione): i dati veri nel database `cied` non vengono toccati. I report passano da
`load_export`, le schede cliniche da `clinical.py`, i CSV dall'esportazione della piattaforma.
`analizza_ilr.py` legge solo i CSV, quindi si può usare anche su un'esportazione reale.
`v_ilr_outcome` contiene `days_monitored` e `days_to_diagnosis`, intervalli in giorni dall'impianto
che si esportano al posto delle date precise.
