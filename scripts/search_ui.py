"""Pagina web LOCALE per la ricerca pazienti, con filtri a tendina e gruppi.

Uso (dalla cartella del progetto, con .venv attivo):
  python scripts/search_ui.py            poi apri http://127.0.0.1:8000 nel browser
  python scripts/search_ui.py --port 8080

La pagina mostra dati identificativi: è raggiungibile solo da questo PC (127.0.0.1)
e usa le credenziali del database del file .env. Ctrl+C per fermarla.
"""
import argparse
import json
import sys
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cied import patients as P  # noqa: E402
from cied.db import connect  # noqa: E402

PAGE_SIZE = 50

HTML = r"""<!DOCTYPE html>
<html lang="it"><head><meta charset="utf-8"><title>Ricerca pazienti CIED</title>
<style>
 body{font-family:system-ui,Segoe UI,sans-serif;margin:24px;color:#1f2933;background:#f7f8fa}
 h1{font-size:20px;margin:0 0 16px}
 .bar{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;background:#fff;padding:16px;border:1px solid #dde1e6;border-radius:8px}
 label{display:flex;flex-direction:column;font-size:12px;color:#52606d;gap:4px}
 input,select,button{font:inherit;padding:6px 8px;border:1px solid #c3cad3;border-radius:6px;background:#fff}
 input[type=text]{min-width:280px} button{cursor:pointer} .primary{background:#1d4ed8;color:#fff;border-color:#1d4ed8}
 table{width:100%;border-collapse:collapse;margin-top:16px;background:#fff;border:1px solid #dde1e6}
 th,td{padding:7px 9px;border-bottom:1px solid #eef0f3;text-align:left;font-size:14px;vertical-align:top}
 th{background:#f0f2f5;font-weight:600;font-size:13px} tr:hover td{background:#f8fafc}
 .muted{color:#7b8794;font-size:13px} .row{display:flex;gap:12px;align-items:center;margin-top:12px;flex-wrap:wrap}
 .pill{display:inline-block;padding:1px 7px;border-radius:10px;font-size:12px;background:#e5e7eb}
 .si{background:#dcfce7;color:#166534} .no{background:#f1f5f9} .nd{background:#fef3c7;color:#92400e}
</style></head><body>
<h1>Ricerca pazienti CIED</h1>
<div class="bar">
 <label>Cerca (cognome, nome, codice, modello, seriale…)
  <input type="text" id="q" placeholder="es. rossi · Biotronik III · biomonitor iv 12345678"></label>
 <label>Produttore <select id="mfg"><option value="">Tutti</option></select></label>
 <label>Modello <select id="model"><option value="">Tutti</option></select></label>
 <label>Gruppo <select id="group"><option value="">Tutti</option></select></label>
 <label>Terapia NAO <select id="nao"><option value="">Tutti</option><option value="SI">Sì</option>
  <option value="NO">No</option><option value="NON_INDICATO">Non indicato</option></select></label>
 <button class="primary" id="apply">Applica filtro</button>
 <button id="reset">Azzera</button>
</div>
<div class="row"><span id="summary" class="muted"></span></div>
<table><thead><tr><th><input type="checkbox" id="all"></th><th>Codice</th><th>Cognome</th><th>Nome</th>
 <th>Nascita</th><th>Sesso</th><th>NAO</th><th>Dispositivi</th><th>Gruppi</th></tr></thead>
 <tbody id="rows"></tbody></table>
<div class="row">
 <button id="prev">‹ Precedenti</button><span id="pageinfo" class="muted"></span><button id="next">Successivi ›</button>
</div>
<div class="row">
 <span class="muted">Pazienti selezionati:</span>
 <select id="target"></select>
 <input type="text" id="newgroup" placeholder="oppure nome nuovo gruppo">
 <button id="addgrp">Aggiungi al gruppo</button>
 <button id="rmgrp">Rimuovi dal gruppo</button>
 <span id="msg" class="muted"></span>
</div>
<script>
const $ = id => document.getElementById(id);
let opts = {manufacturers: [], models: [], groups: []}, page = 0, pages = 1;

function fill(sel, items, keep) {
  const cur = keep ? sel.value : "";
  sel.length = 1;
  for (const [v, t] of items) sel.add(new Option(t, v));
  if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
}
function fillModels() {
  const m = $("mfg").value;
  fill($("model"), opts.models.filter(x => !m || x.manufacturer === m)
       .map(x => [x.model, x.model + " (" + x.patients + ")"]), true);
}
async function loadOptions() {
  opts = await (await fetch("/api/options")).json();
  fill($("mfg"), opts.manufacturers.map(x => [x.code, x.name + " (" + x.patients + ")"]), true);
  fillModels();
  fill($("group"), opts.groups.map(x => [x.group_pk, x.name + " (" + x.patients + ")"]), true);
  $("target").length = 0;
  for (const g of opts.groups) $("target").add(new Option(g.name, g.group_pk));
}
function cell(tr, text, cls) {
  const td = tr.insertCell();
  if (cls) { const s = document.createElement("span"); s.className = "pill " + cls; s.textContent = text; td.append(s); }
  else td.textContent = text ?? "";
  return td;
}
async function search(p) {
  page = p ?? 0;
  const qs = new URLSearchParams({q: $("q").value, mfg: $("mfg").value, model: $("model").value,
                                  group: $("group").value, nao: $("nao").value, page});
  const r = await fetch("/api/search?" + qs);
  const d = await r.json();
  if (!r.ok) { $("summary").textContent = "Errore: " + d.error; return; }
  pages = Math.max(1, Math.ceil(d.total / d.page_size));
  $("summary").textContent = d.total + " pazienti trovati";
  $("pageinfo").textContent = "pagina " + (page + 1) + " di " + pages;
  $("prev").disabled = page === 0; $("next").disabled = page + 1 >= pages;
  const tb = $("rows"); tb.replaceChildren(); $("all").checked = false;
  for (const x of d.rows) {
    const tr = tb.insertRow();
    const cb = document.createElement("input"); cb.type = "checkbox"; cb.value = x.patient_no;
    tr.insertCell().append(cb);
    cell(tr, x.patient_no); cell(tr, x.name_family); cell(tr, x.name_given);
    cell(tr, x.birth_date ? x.birth_date.split("-").reverse().join("/") : "");
    cell(tr, x.sex);
    if (x.nao_therapy === true) cell(tr, "Sì", "si");
    else if (x.nao_therapy === false) cell(tr, "No", "no"); else cell(tr, "n.i.", "nd");
    cell(tr, x.devices); cell(tr, x.groups);
  }
}
function selected() { return [...document.querySelectorAll("#rows input:checked")].map(c => +c.value); }
async function groupAction(action) {
  const patients = selected();
  if (!patients.length) { $("msg").textContent = "Seleziona almeno un paziente."; return; }
  const body = {action, patients, group: $("newgroup").value.trim() || +$("target").value};
  if (!body.group) { $("msg").textContent = "Scegli un gruppo o scrivi il nome di uno nuovo."; return; }
  const r = await fetch("/api/groups", {method: "POST", headers: {"Content-Type": "application/json"},
                                         body: JSON.stringify(body)});
  const d = await r.json();
  $("msg").textContent = r.ok ? d.message : "Errore: " + d.error;
  $("newgroup").value = "";
  await loadOptions(); await search(page);
}
$("mfg").onchange = fillModels;
$("apply").onclick = () => search(0);
$("q").onkeydown = e => { if (e.key === "Enter") search(0); };
$("reset").onclick = () => { $("q").value = ""; for (const id of ["mfg", "group", "nao"]) $(id).value = "";
                             fillModels(); $("model").value = ""; search(0); };
$("prev").onclick = () => search(page - 1); $("next").onclick = () => search(page + 1);
$("all").onchange = e => document.querySelectorAll("#rows input").forEach(c => c.checked = e.target.checked);
$("addgrp").onclick = () => groupAction("add"); $("rmgrp").onclick = () => groupAction("remove");
loadOptions().then(() => search(0));
</script></body></html>"""


def _json_default(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(type(o))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):   # niente log su terminale: le URL contengono le ricerche
        pass

    def _send(self, status, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=_json_default).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _local(self) -> bool:
        """Solo richieste dirette a questo PC (protegge da siti esterni che provano a usare la pagina)."""
        port = self.server.server_address[1]
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self._send(403, {"error": "accesso consentito solo da questo PC"})
            return False
        origin = self.headers.get("Origin")
        if self.command == "POST" and origin not in (None, f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            self._send(403, {"error": "origine non consentita"})
            return False
        return True

    def do_GET(self):
        if not self._local():
            return
        u = urlparse(self.path)
        try:
            if u.path == "/":
                return self._send(200, HTML.encode(), "text/html; charset=utf-8")
            if u.path == "/api/options":
                with connect() as c:
                    return self._send(200, P.search_options(c))
            if u.path == "/api/search":
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                page = max(int(q.get("page") or 0), 0)
                with connect() as c:
                    rows, total = P.search(c, q.get("q") or None, manufacturer=q.get("mfg") or None,
                                           model=q.get("model") or None,
                                           group=int(q["group"]) if q.get("group") else None,
                                           nao=q.get("nao") or None, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
                return self._send(200, {"rows": rows, "total": total, "page": page, "page_size": PAGE_SIZE})
            self._send(404, {"error": "pagina inesistente"})
        except ValueError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        if not self._local():
            return
        if urlparse(self.path).path != "/api/groups":
            return self._send(404, {"error": "pagina inesistente"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            patients = [int(x) for x in body.get("patients", [])]
            group = body.get("group")
            with connect() as c:
                if body.get("action") == "add":
                    if isinstance(group, str):
                        group = P.create_group(c, group)
                    n = P.add_to_group(c, group, patients)
                    return self._send(200, {"message": f"{n} pazienti aggiunti al gruppo."})
                if body.get("action") == "remove":
                    n = P.remove_from_group(c, group, patients)
                    return self._send(200, {"message": f"{n} pazienti rimossi dal gruppo."})
            self._send(400, {"error": "azione non valida"})
        except ValueError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


def make_server(port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    srv = make_server(args.port)
    print(f"Ricerca pazienti: apri http://127.0.0.1:{args.port} nel browser (Ctrl+C per fermare)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nFermato.")
        srv.server_close()
