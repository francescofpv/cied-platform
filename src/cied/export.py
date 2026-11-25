"""Esportazione CSV con il COPY nativo di PostgreSQL.

Il database scrive direttamente il CSV (intestazione, virgolette, NULL, date);
Python si limita a passare i byte nel file. Si esportano solo tabelle e viste
dello schema pseudonimizzato `cied`: i dati identificativi di `vault` non escono.

Si può esportare tutto oppure solo i pazienti di un gruppo (cied.patient_group).

Per ridurre il rischio di riconoscere un paziente, nel CSV non esce mai l'anno
di nascita ma l'età, e delle date di impianto esce solo l'anno.
"""
from pathlib import Path

from psycopg import sql

from cied.patients import group_pk_of

# Come si risale al paziente partendo da una colonna della tabella o vista da
# esportare. Si usa la prima colonna presente, nell'ordine dell'elenco.
# {m} = sottoquery con i patient_pk dei membri del gruppo.
_GROUP_FILTERS = [
    ("patient_pk",     "{c} IN ({m})"),
    ("patient_pseudo", "{c} IN (SELECT patient_pseudo FROM cied.patient WHERE patient_pk IN ({m}))"),
    ("device_pk",      "{c} IN (SELECT device_pk FROM cied.device WHERE patient_pk IN ({m}))"),
    ("device_pseudo",  "{c} IN (SELECT device_pseudo FROM cied.device WHERE patient_pk IN ({m}))"),
    ("session_pk",     "{c} IN (SELECT s.session_pk FROM cied.session s JOIN cied.device d USING (device_pk)"
                       " WHERE d.patient_pk IN ({m}))"),
    ("implant_pk",     "{c} IN (SELECT implant_pk FROM cied.implant WHERE patient_pk IN ({m}))"),
    ("ilr_implant_pk", "{c} IN (SELECT implant_pk FROM cied.implant WHERE patient_pk IN ({m}))"),
]


# Colonne che non escono così come sono, per ridurre il rischio di riconoscere
# il paziente: al posto dell'anno di nascita si esporta l'età, al posto della
# data di impianto solo l'anno. colonna -> (espressione SQL, nome nel CSV)
_GENERALIZED = {
    "birth_year":        ("extract(year FROM current_date)::int - {c}", "age"),
    "implant_date":      ("extract(year FROM {c})::int", "implant_year"),
    "implant_dt":        ("extract(year FROM {c})::int", "implant_year"),
    "ilr_implant_date":  ("extract(year FROM {c})::int", "ilr_implant_year"),
    "next_implant_date": ("extract(year FROM {c})::int", "next_implant_year"),
}


def _columns(conn, name: str) -> list[str]:
    return [r[0] for r in conn.execute(
        """SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'cied' AND table_name = %s ORDER BY ordinal_position""", (name,))]


# Anche il report del dispositivo trasmette le date di impianto come valori
# (MDC_IDC_DEV_IMPLANT_DT, e per gli elettrodi MDC_IDC_LEAD_IMPLANT_DT): nelle
# righe che hanno un codice IEEE ("term") queste date escono ridotte all'anno.
_IMPLANT_TERM = "{t} LIKE 'MDC_IDC_%IMPLANT_DT'"
_GENERALIZED_BY_TERM = {
    "value_text": "CASE WHEN " + _IMPLANT_TERM + " THEN left({c}, 4) ELSE {c} END",
    "value_dtm":  "CASE WHEN " + _IMPLANT_TERM + " THEN NULL ELSE {c} END",
}


def _select_list(columns: list[str]) -> sql.Composable:
    """Elenco delle colonne da esportare, con età e anno al posto delle date."""
    items = []
    for col in columns:
        if "term" in columns and col in _GENERALIZED_BY_TERM:
            items.append(sql.SQL(_GENERALIZED_BY_TERM[col] + " AS {c}").format(
                t=sql.Identifier("term"), c=sql.Identifier(col)))
        elif col in _GENERALIZED:
            expr, alias = _GENERALIZED[col]
            items.append(sql.SQL(expr + " AS {a}").format(c=sql.Identifier(col), a=sql.Identifier(alias)))
        else:
            items.append(sql.Identifier(col))
    return sql.SQL(", ").join(items)


def exportable(conn) -> list[str]:
    """Tabelle e viste dello schema cied, in ordine alfabetico."""
    return [r[0] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'cied' ORDER BY 1")]


def groups(conn) -> list[tuple[str, int]]:
    """Gruppi di pazienti con il numero di membri, in ordine alfabetico."""
    return conn.execute("""SELECT g.name, count(m.patient_pk)::int FROM cied.patient_group g
                           LEFT JOIN cied.patient_group_member m USING (group_pk)
                           GROUP BY g.name ORDER BY g.name""").fetchall()


def _group_filter(conn, name: str, group_pk: int) -> sql.Composable:
    """Condizione WHERE che tiene solo le righe dei pazienti del gruppo."""
    cols = set(_columns(conn, name))
    members = sql.SQL("SELECT patient_pk FROM cied.patient_group_member WHERE group_pk = {}").format(
        sql.Literal(group_pk))
    for col, template in _GROUP_FILTERS:
        if col in cols:
            return sql.SQL(template).format(c=sql.Identifier(col), m=members)
    raise ValueError(f"'{name}' non contiene dati dei pazienti: non si può filtrare per gruppo "
                     "(esportala senza indicare il gruppo)")


def export_csv(conn, name: str, path: Path, excel: bool = False, group: int | str | None = None) -> int:
    """Scrive cied.<name> in un file CSV e restituisce il numero di righe.

    excel=True: separatore ";" e BOM UTF-8, così Excel in italiano apre il file
    con le colonne giuste e le lettere accentate corrette.
    group: nome (o group_pk) di un gruppo di pazienti; se indicato si esportano
    solo le righe dei pazienti di quel gruppo.
    """
    if name not in exportable(conn):
        raise ValueError(f"'{name}' non è una tabella o vista dello schema cied")
    select = sql.SQL("SELECT {} FROM {}").format(_select_list(_columns(conn, name)), sql.Identifier("cied", name))
    if group is not None:
        select = sql.SQL("{} WHERE {}").format(select, _group_filter(conn, name, group_pk_of(conn, group)))
    query = sql.SQL("COPY ({}) TO STDOUT (FORMAT csv, HEADER, DELIMITER {})").format(
        select, sql.Literal(";" if excel else ","))
    with open(path, "wb") as f, conn.cursor() as cur:
        if excel:
            f.write(b"\xef\xbb\xbf")
        with cur.copy(query) as copy:
            for data in copy:
                f.write(data)
        return cur.rowcount
