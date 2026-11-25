"""Sincronizzazione automatica: API Home Monitoring -> database.

Due modalità, entrambe previste dalla documentazione BIOTRONIK:

1. CODA (sync_jobs), il flusso standard:
   GET /exports  ->  per ogni export: GET /exports/{id}  ->  import  ->  DELETE /exports/{id}
   L'export si segna come processato (DELETE) SOLO dopo che l'import è riuscito, oppure
   se era un duplicato. Se l'import fallisce resta in coda e si riprova al giro dopo
   (attenzione: BIOTRONIK cancella gli export dopo 14 giorni).

2. ON DEMAND (sync_on_demand):
   per ogni dispositivo BIOTRONIK già noto chiede GET /exports/latest con la data
   dell'ultimo report ricevuto.

Le due modalità possono restituire lo stesso report più volte: non è un problema,
perché l'import riconosce la sessione già presente (chiave naturale) e non la
importa di nuovo.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from cied.db.loader import DUPLICATE, LOADED, MESSAGES, load_export
from cied.hm_client import HMClient, HMError

SINCE_ALWAYS = datetime(2000, 1, 1, tzinfo=timezone.utc)   # per un dispositivo senza report


@dataclass
class SyncResult:
    seen: int = 0            # export (o dispositivi) esaminati
    loaded: int = 0          # importati
    duplicates: int = 0      # già presenti
    up_to_date: int = 0      # on demand: nessun report nuovo
    acknowledged: int = 0    # segnati come processati (DELETE)
    errors: list[str] = field(default_factory=list)

    def count(self, status: str) -> None:
        if status == LOADED:
            self.loaded += 1
        elif status == DUPLICATE:
            self.duplicates += 1

    def as_dict(self) -> dict:
        return {"visti": self.seen, "importati": self.loaded, "duplicati": self.duplicates,
                "gia_aggiornati": self.up_to_date, "segnati_processati": self.acknowledged,
                "errori": len(self.errors)}


def sync_jobs(client: HMClient, conn, pseudo_key: str, *, acknowledge: bool = True,
              log: Callable[[str], None] = print) -> SyncResult:
    """Scarica e importa gli export in coda."""
    result = SyncResult()
    jobs = client.list_exports()
    log(f"Export in coda: {len(jobs)}")

    for job in jobs:
        result.seen += 1
        label = f"export {job.id}"
        try:
            r = load_export(conn, client.get_export(job.id), pseudo_key, vendor_export_id=job.id)
        except Exception as e:          # errore API, XML non valido, errore DB: resta in coda
            result.errors.append(f"{label}: {e}")
            log(f"  {label}: ERRORE {e} (resta in coda)")
            continue

        result.count(r["status"])
        log(f"  {label}: {MESSAGES[r['status']]}")

        if acknowledge:
            try:
                client.delete_export(job.id)
                result.acknowledged += 1
            except HMError as e:
                result.errors.append(f"{label}: DELETE fallito: {e}")
                log(f"  {label}: importato ma non segnato come processato: {e}")
    return result


def known_devices(conn, serials: list[str] | None = None) -> list[tuple[str, datetime]]:
    """Dispositivi BIOTRONIK noti, con la data dell'ultimo report ricevuto.

    La data è quella della richiesta dell'ultimo export (BIO/REQUEST/DATE);
    se manca si usa la sessione più recente.
    """
    rows = conn.execute(
        """SELECT di.serial_number, COALESCE(max(e.request_dtm), max(s.session_dtm))
             FROM vault.device_identity di
             JOIN cied.device d ON d.device_pseudo = di.device_pseudo
             LEFT JOIN cied.export e  ON e.device_pk = d.device_pk
             LEFT JOIN cied.session s ON s.device_pk = d.device_pk
            WHERE di.manufacturer = 'BIO'
            GROUP BY di.serial_number
            ORDER BY di.serial_number""").fetchall()
    last_report = {serial: last or SINCE_ALWAYS for serial, last in rows}
    if serials:
        return [(s, last_report.get(s, SINCE_ALWAYS)) for s in serials]
    return list(last_report.items())


def sync_on_demand(client: HMClient, conn, pseudo_key: str, serials: list[str] | None = None, *,
                   log: Callable[[str], None] = print) -> SyncResult:
    """Chiede l'ultimo report di ogni dispositivo noto (o dei seriali indicati)."""
    result = SyncResult()
    devices = known_devices(conn, serials)
    log(f"Dispositivi da controllare: {len(devices)}")

    for serial, last in devices:
        result.seen += 1
        label = f"dispositivo …{serial[-3:]}"     # il seriale è identificativo: nel log solo le ultime cifre
        try:
            data = client.latest_export(serial, last)
            if data is None:
                result.up_to_date += 1
                log(f"  {label}: nessun report dopo {last:%Y-%m-%d %H:%M}")
                continue
            r = load_export(conn, data, pseudo_key)
        except Exception as e:
            result.errors.append(f"{label}: {e}")
            log(f"  {label}: ERRORE {e}")
            continue
        result.count(r["status"])
        log(f"  {label}: {MESSAGES[r['status']]}")
    return result
