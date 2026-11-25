"""Client per il BIOTRONIK Home Monitoring Web Service (EHR DataSync, versione 1).

Riferimento: "EHR DataSync - Web Service Documentation", Rev. G (2023-11-22), cap. 5.

Accesso (sez. 5.2): servono SEMPRE entrambi
  - certificato client (mTLS), rilasciato da BIOTRONIK come bundle .p12
    (convertirlo in cert.pem + private.key con scripts/convert_p12.py);
  - Basic Auth con Client ID e Client secret dell'export client.

Endpoint (sez. 5.3):
  GET    /exports                                   elenco degli export in coda (JSON)
  GET    /exports/{id}                              XML dell'export
  DELETE /exports/{id}                              segna l'export come "processato" (204)
  GET    /exports/latest;implant={seriale};newer_than={YYYY-MM-DDThh:mm:ssZ}
                                                    report più recente dopo la data (200) o nulla (204)

Limite consigliato: max 5 richieste al minuto per export client (sez. 4 e 5).
Il client resta sotto il limite con un rate limiter a finestra mobile e, se il
server risponde comunque 429/503/500, aspetta e riprova.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import requests

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"   # formato delle date dell'API (UTC, sez. 5.3.4 e 5.4.1)


class HMError(Exception):
    """Risposta di errore del web service (sez. 5.4.2 e 5.5)."""

    HINTS = {
        "4000": "parametro non valido (seriale o data errati)",
        "4010": "certificato client mancante o non valido",
        "4011": "Client ID o Client secret errati",
        "4030": "accesso al web service non abilitato per questo gruppo utenti",
        "4040": "risorsa inesistente o non autorizzata per questo export client",
        "4290": "troppe richieste: superato il limite",
        "5030": "web service in manutenzione",
    }

    def __init__(self, status: int, errors: list[dict], url: str):
        self.status, self.errors, self.url = status, errors, url
        detail = "; ".join(
            f"{e.get('code')} {e.get('message', '')}".strip()
            + (f" ({self.HINTS[str(e.get('code'))]})" if str(e.get("code")) in self.HINTS else "")
            for e in errors
        ) or "nessun dettaglio"
        super().__init__(f"HTTP {status} su {url}: {detail}")

    @property
    def codes(self) -> set[str]:
        return {str(e.get("code")) for e in self.errors}


@dataclass(frozen=True)
class ExportJob:
    id: str
    patient: str | None          # ID del paziente nel Service Center (dato identificativo)
    created_at: datetime | None  # UTC


def format_ts(dt: datetime) -> str:
    """Data/ora nel formato dell'API, convertita in UTC. Una data senza fuso è considerata UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(TS_FORMAT)


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class RateLimiter:
    """Al massimo `max_calls` chiamate in una finestra mobile di `period` secondi."""

    def __init__(self, max_calls: int, period: float = 60.0,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        if max_calls < 1:
            raise ValueError("max_calls deve essere >= 1")
        self.max_calls, self.period, self.clock, self.sleep = max_calls, period, clock, sleep
        self._calls: deque[float] = deque()

    def acquire(self) -> float:
        """Attende se serve; restituisce i secondi attesi."""
        waited = 0.0
        while True:
            now = self.clock()
            while self._calls and self._calls[0] <= now - self.period:
                self._calls.popleft()
            if len(self._calls) < self.max_calls:
                self._calls.append(now)
                return waited
            wait = self._calls[0] + self.period - now
            self.sleep(wait)
            waited += wait


class HMClient:
    RETRY_STATUS = {429, 500, 503}

    def __init__(self, base_url: str, client_id: str, client_secret: str,
                 cert_pem: str | Path, key_pem: str | Path, *,
                 ca_bundle: str | Path | None = None, max_per_min: int = 4, timeout: float = 60,
                 retries: int = 3, retry_wait: float = 60.0,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 session: requests.Session | None = None):
        if not client_id or not client_secret:
            raise ValueError("HM_CLIENT_ID e HM_CLIENT_SECRET sono obbligatori (vedi .env)")
        for label, p in (("HM_CERT_PEM", cert_pem), ("HM_KEY_PEM", key_pem)):
            if not Path(p).is_file():
                raise FileNotFoundError(f"{label}: file non trovato: {p} "
                                        "(converti il .p12 con: python scripts/convert_p12.py)")
        self.base_url = base_url.rstrip("/")
        self.timeout, self.retries, self.retry_wait, self.sleep = timeout, retries, retry_wait, sleep
        self.limiter = RateLimiter(max_per_min, 60.0, clock=clock, sleep=sleep)
        self.s = session or requests.Session()
        self.s.auth = (client_id, client_secret)            # Basic Auth (sez. 5.2.2)
        # certificato client (sez. 5.2.1) e CA del server (HM_CA_BUNDLE solo per il server finto).
        # Si passano a OGNI richiesta: in requests le variabili d'ambiente (REQUESTS_CA_BUNDLE,
        # proxy aziendali...) prevalgono sulle impostazioni della sessione, non su quelle della richiesta.
        self.cert = (str(cert_pem), str(key_pem))
        self.verify = str(ca_bundle) if ca_bundle else True
        self.s.headers["User-Agent"] = "cied-platform/0.2"
        self.requests_made = 0

    @classmethod
    def from_settings(cls, settings, **kw) -> "HMClient":
        return cls(settings.hm_base_url, settings.hm_client_id, settings.hm_client_secret,
                   settings.hm_cert_pem, settings.hm_key_pem,
                   ca_bundle=settings.hm_ca_bundle or None, max_per_min=settings.hm_max_req_per_min, **kw)

    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, ok: tuple[int, ...] = (200,)) -> requests.Response:
        url = self.base_url + path
        for attempt in range(self.retries + 1):
            self.limiter.acquire()
            self.requests_made += 1
            r = self.s.request(method, url, timeout=self.timeout, cert=self.cert, verify=self.verify)
            if r.status_code in ok:
                return r
            if r.status_code in self.RETRY_STATUS and attempt < self.retries:
                self.sleep(self._wait_for(r, attempt))
                continue
            raise HMError(r.status_code, _errors(r), url)
        raise AssertionError("non raggiungibile")

    def _wait_for(self, r: requests.Response, attempt: int) -> float:
        ra = r.headers.get("Retry-After", "")
        if ra.isdigit():
            return float(ra)
        return self.retry_wait * (attempt + 1)

    # --- endpoint -------------------------------------------------------
    def list_exports(self) -> list[ExportJob]:
        data = self._request("GET", "/exports").json()
        return [ExportJob(id=e["id"], patient=e.get("patient"), created_at=parse_ts(e.get("created_at")))
                for e in data.get("exports", [])]

    def get_export(self, export_id: str) -> bytes:
        """XML grezzo (bytes, di solito UTF-16): va passato così com'è a load_export."""
        return self._request("GET", f"/exports/{quote(export_id, safe='')}").content

    def delete_export(self, export_id: str) -> None:
        """Segna l'export come processato per QUESTO client (non lo cancella per gli altri)."""
        self._request("DELETE", f"/exports/{quote(export_id, safe='')}", ok=(204, 200))

    def latest_export(self, serial: str, newer_than: datetime) -> bytes | None:
        """Report più recente del dispositivo successivo a `newer_than`; None se non c'è (204)."""
        path = f"/exports/latest;implant={quote(serial, safe='')};newer_than={format_ts(newer_than)}"
        r = self._request("GET", path, ok=(200, 204))
        return None if r.status_code == 204 or not r.content else r.content


def _errors(r: requests.Response) -> list[dict]:
    try:
        errs = r.json().get("errors", [])
        return errs if isinstance(errs, list) else []
    except ValueError:
        return []
