"""Server FINTO del BIOTRONIK Home Monitoring Web Service, per sviluppo e test.

Riproduce il comportamento descritto nella documentazione EHR DataSync (Rev. G):
  - HTTPS con certificato client obbligatorio (mTLS): senza certificato la connessione fallisce;
  - Basic Auth con Client ID e Client secret (errore 401 / 4011 se errati);
  - i 4 endpoint: GET /exports, GET /exports/{id}, DELETE /exports/{id},
    GET /exports/latest;implant=...;newer_than=...;
  - errori in JSON {"errors":[{"code":..., "message":...}]} con i codici della sez. 5.5;
  - limite di richieste al minuto (429 / 4290), di default 5 come da documentazione;
  - risposte XML in UTF-16, come l'API reale.

Le "chiavi" usate qui (CA, certificato server e client) sono generate al volo
e valgono solo per il server finto: non hanno nulla a che fare con quelle reali.
"""
from __future__ import annotations

import base64
import datetime as dt
import ipaddress
import json
import re
import ssl
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from cied.hm_client import TS_FORMAT, format_ts
from cied.parser import parse_export
from cied.parser.ieee11073 import xml_text

API_PREFIX = "/rest/api"
_LATEST = re.compile(r"^/exports/latest;implant=([^;]+);newer_than=([^;/]+)$")

ERRORS = {
    4000: (400, "Invalid parameter."),
    4011: (401, "Invalid or missing credentials."),
    4040: (404, "The requested resource does not exist."),
    4290: (429, "Too many requests"),
    5030: (503, "The HMSC web service is currently unavailable."),
}


# --------------------------------------------------------------------------
# Certificati di prova
# --------------------------------------------------------------------------
def make_test_pki(folder: str | Path, p12_password: str = "test") -> dict[str, Path]:
    """Crea CA, certificato server (localhost) e certificato client + bundle .p12 di prova."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)

    def name(cn):
        return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "CIED mock (solo test)"),
                          x509.NameAttribute(NameOID.COMMON_NAME, cn)])

    def build(subject, key, issuer, issuer_key, *, ca=False, eku=None, san=None):
        b = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
             .public_key(key.public_key()).serial_number(x509.random_serial_number())
             .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=730))
             .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
             # Python >= 3.13 verifica in modo rigoroso: servono SKI/AKI e KeyUsage
             .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
             .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
                            critical=False)
             .add_extension(x509.KeyUsage(digital_signature=not ca, key_cert_sign=ca, crl_sign=ca,
                                          content_commitment=False, key_encipherment=False,
                                          data_encipherment=False, key_agreement=not ca,
                                          encipher_only=False, decipher_only=False), critical=True))
        if eku:
            b = b.add_extension(x509.ExtendedKeyUsage(eku), critical=False)
        if san:
            b = b.add_extension(x509.SubjectAlternativeName(san), critical=False)
        return b.sign(issuer_key, hashes.SHA256())

    def write_key(path, key):
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))

    def write_cert(path, cert):
        path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = build(name("CIED Mock CA"), ca_key, name("CIED Mock CA"), ca_key, ca=True)
    srv_key = ec.generate_private_key(ec.SECP256R1())
    srv = build(name("localhost"), srv_key, ca.subject, ca_key, eku=[ExtendedKeyUsageOID.SERVER_AUTH],
                san=[x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))])
    cli_key = ec.generate_private_key(ec.SECP256R1())
    cli = build(name("mock-export-client"), cli_key, ca.subject, ca_key, eku=[ExtendedKeyUsageOID.CLIENT_AUTH])

    p = {k: folder / v for k, v in {
        "ca": "ca.pem", "server_cert": "server.pem", "server_key": "server.key",
        "client_cert": "client.pem", "client_key": "client.key", "client_p12": "client.p12"}.items()}
    write_cert(p["ca"], ca)
    write_cert(p["server_cert"], srv)
    write_key(p["server_key"], srv_key)
    write_cert(p["client_cert"], cli)
    write_key(p["client_key"], cli_key)
    p["client_p12"].write_bytes(pkcs12.serialize_key_and_certificates(
        b"mock-export-client", cli_key, cli, [ca],
        serialization.BestAvailableEncryption(p12_password.encode())))
    return p


# --------------------------------------------------------------------------
# Dati serviti
# --------------------------------------------------------------------------
@dataclass
class MockExport:
    id: str
    patient: str | None
    serial: str | None
    report_dtm: dt.datetime     # UTC: data del report (BIO/REQUEST/DATE, altrimenti sessione)
    created_at: dt.datetime     # UTC
    data: bytes                 # XML in UTF-16 con BOM, come l'API reale
    processed: bool = False


def _report_dtm(exp) -> dt.datetime:
    for path in ("BIO/REQUEST/DATE", "MDC/IDC/SESS/DTM"):
        v = exp.get(path)
        if v and v.dtm:
            d = v.dtm if v.tz_known else v.dtm.replace(tzinfo=dt.timezone.utc)
            return d.astimezone(dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc)


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------
class MockHMServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, pki: dict[str, Path], host: str = "127.0.0.1", port: int = 0, *,
                 client_id: str = "mock-client", client_secret: str = "mock-secret",
                 max_per_min: int | None = 5, quiet: bool = True):
        super().__init__((host, port), _Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(pki["server_cert"], pki["server_key"])
        ctx.load_verify_locations(pki["ca"])
        ctx.verify_mode = ssl.CERT_REQUIRED          # mTLS: certificato client obbligatorio
        self.socket = ctx.wrap_socket(self.socket, server_side=True)
        self.client_id, self.client_secret = client_id, client_secret
        self.max_per_min, self.quiet = max_per_min, quiet
        self.exports: dict[str, MockExport] = {}
        self.forced: deque[int] = deque()            # codici di errore da restituire alle prossime richieste
        self.log: list[tuple[str, str, int]] = []    # (metodo, path, status)
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"https://127.0.0.1:{self.server_address[1]}{API_PREFIX}"

    def add_export(self, data: bytes, created_at: dt.datetime | None = None) -> MockExport:
        exp = parse_export(data)
        sv, pv = exp.get("MDC/IDC/DEV/SERIAL"), exp.get("MDC/ATTR/PT/ID")
        rep = _report_dtm(exp)
        e = MockExport(id=uuid.uuid4().hex, patient=pv.raw if pv else None, serial=sv.raw if sv else None,
                       report_dtm=rep, created_at=created_at or rep,
                       data=xml_text(data).encode("utf-16"))
        with self._lock:
            self.exports[e.id] = e
        return e

    def add_folder(self, folder: str | Path) -> list[MockExport]:
        return [self.add_export(f.read_bytes()) for f in sorted(Path(folder).glob("*.xml"))]

    def start(self) -> "MockHMServer":
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()

    def handle_error(self, request, client_address):  # handshake fallito ecc.: niente traceback rumorosi
        if not self.quiet:
            super().handle_error(request, client_address)

    def _rate_ok(self) -> bool:
        if not self.max_per_min:
            return True
        now = time.monotonic()
        with self._lock:
            while self._calls and self._calls[0] <= now - 60:
                self._calls.popleft()
            if len(self._calls) >= self.max_per_min:
                return False
            self._calls.append(now)
            return True


class _Handler(BaseHTTPRequestHandler):
    server: MockHMServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if not self.server.quiet:
            super().log_message(fmt, *args)

    # --- risposte ---
    def _send(self, status: int, body: bytes = b"", ctype: str | None = None, headers: dict | None = None):
        self.server.log.append((self.command, self.path, status))
        self.send_response(status)
        if ctype:
            self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _error(self, code: int):
        status, msg = ERRORS[code]
        headers = {"Retry-After": "60"} if status == 429 else None
        self._send(status, json.dumps({"errors": [{"message": msg, "code": str(code)}]}).encode(),
                   "application/json", headers)

    def _authorized(self) -> bool:
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            user, _, pwd = base64.b64decode(h[6:]).decode().partition(":")
        except Exception:
            return False
        return (user, pwd) == (self.server.client_id, self.server.client_secret)

    def _route(self) -> str | None:
        path = self.path.split("?", 1)[0]
        if not path.startswith(API_PREFIX):
            self._error(4040)
            return None
        if self.server.forced:
            self._error(self.server.forced.popleft())
            return None
        if not self._authorized():
            self._error(4011)
            return None
        if not self.server._rate_ok():
            self._error(4290)
            return None
        return path[len(API_PREFIX):]

    # --- endpoint ---
    def do_GET(self):
        path = self._route()
        if path is None:
            return
        if path == "/exports":
            with self.server._lock:
                items = [{"patient": e.patient, "id": e.id, "created_at": format_ts(e.created_at)}
                         for e in sorted(self.server.exports.values(), key=lambda x: x.created_at)
                         if not e.processed]
            return self._send(200, json.dumps({"exports": items}).encode(), "application/json")
        m = _LATEST.match(path)
        if m:
            serial, ts = unquote(m.group(1)), unquote(m.group(2))
            try:
                newer = dt.datetime.strptime(ts, TS_FORMAT).replace(tzinfo=dt.timezone.utc)
            except ValueError:
                return self._error(4000)
            with self.server._lock:
                mine = [e for e in self.server.exports.values() if e.serial == serial]
            if not mine:
                return self._error(4000)        # seriale non riconosciuto (scenario 3, sez. 5.3.4)
            newer_ones = [e for e in mine if e.report_dtm > newer]
            if not newer_ones:
                return self._send(204)
            best = max(newer_ones, key=lambda e: e.report_dtm)
            return self._send(200, best.data, "application/xml; charset=UTF-16")
        if path.startswith("/exports/"):
            e = self.server.exports.get(unquote(path[len("/exports/"):]))
            if e is None or e.processed:
                return self._error(4040)
            return self._send(200, e.data, "application/xml; charset=UTF-16")
        self._error(4040)

    def do_DELETE(self):
        path = self._route()
        if path is None:
            return
        if path.startswith("/exports/") and not _LATEST.match(path):
            e = self.server.exports.get(unquote(path[len("/exports/"):]))
            if e is None or e.processed:
                return self._error(4040)
            e.processed = True
            return self._send(204)
        self._error(4040)
