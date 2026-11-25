"""Parser degli export BIOTRONIK "biotronik-ieee11073-export" (format-version 4.x).

L'XML è un albero di <section name="..."> annidate che contengono dei <value>.
Il parser lo "appiattisce" in una lista di valori (ExportValue), ognuno con:

  path   percorso completo; le sezioni ripetute hanno un indice
         es. MDC/IDC/STAT/EPISODE[3]/TOTAL_COUNT
  group  percorso della sezione che lo contiene (serve a tenere insieme i
         valori della stessa zona, dello stesso episodio, ecc.)
  code   codice numerico IEEE 11073-10103 (solo nel ramo MDC)
  raw    valore come scritto nel file, più la versione tipizzata (num / dtm)

Codifica: gli export originali (Service Center e API) sono UTF-16 con BOM.
Una copia risalvata in un'altra codifica non è più il file firmato da
BIOTRONIK e viene rifiutata.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from defusedxml import ElementTree as ET

XML_SIGNATURE = "{http://www.w3.org/2000/09/xmldsig#}Signature"


@dataclass
class ExportValue:
    path: str
    group: str
    name: str
    type: str | None
    raw: str | None
    code: int | None = None
    enum_code: int | None = None
    unit: str | None = None
    num: float | None = None          # valore se type = Numeric
    dtm: datetime | None = None       # valore se type = DateTime
    tz_known: bool | None = None      # False = data/ora scritta senza fuso orario


@dataclass
class ParsedExport:
    format_version: str | None
    creator: str | None
    creator_version: str | None
    signed: bool
    values: list[ExportValue] = field(default_factory=list)

    def get(self, path: str) -> ExportValue | None:
        """Il valore con questo percorso, oppure None."""
        for v in self.values:
            if v.path == path:
                return v
        return None

    def text(self, path: str) -> str | None:
        """Il testo del valore con questo percorso, oppure None."""
        v = self.get(path)
        return v.raw if v else None


def xml_text(data: bytes) -> str:
    """Testo dell'XML originale, da archiviare in vault.export_raw."""
    if data[:2] not in (b"\xff\xfe", b"\xfe\xff"):
        raise ValueError("L'export non è in UTF-16 con BOM: non è un file originale Biotronik")
    return data.decode("utf-16")


def parse_datetime(raw: str) -> tuple[datetime | None, bool | None]:
    """'20261023T022001+0200' -> (data con fuso, True); '20261003T013302' -> (data senza fuso, False)."""
    try:
        return datetime.strptime(raw, "%Y%m%dT%H%M%S%z"), True
    except ValueError:
        pass
    try:
        return datetime.strptime(raw, "%Y%m%dT%H%M%S"), False
    except ValueError:
        return None, None


def to_int(s: str | None) -> int | None:
    return int(s) if s and s.isdigit() else None


def read_value(elem, group: str) -> ExportValue:
    raw = elem.text.strip() if elem.text else None
    v = ExportValue(path=f"{group}/{elem.get('name')}", group=group, name=elem.get("name"),
                    type=elem.get("type"), raw=raw, code=to_int(elem.get("code")),
                    enum_code=to_int(elem.get("enumeration_code")), unit=elem.get("unit"))
    if raw is not None and v.type == "Numeric":
        try:
            v.num = float(raw)
        except ValueError:
            pass
    if raw is not None and v.type == "DateTime":
        v.dtm, v.tz_known = parse_datetime(raw)
    return v


def walk(section, path: str, out: list[ExportValue]) -> None:
    """Visita una sezione e le sue sottosezioni, aggiungendo i valori a `out`."""
    # quante volte compare ogni nome di sottosezione: le ripetute ricevono un indice [1], [2], ...
    how_many = Counter(child.get("name") for child in section if child.tag == "section")
    seen = Counter()
    for child in section:
        if child.tag == "value":
            out.append(read_value(child, path))
        elif child.tag == "section":
            name = child.get("name")
            seen[name] += 1
            segment = f"{name}[{seen[name]}]" if how_many[name] > 1 else name
            walk(child, f"{path}/{segment}" if path else segment, out)


def parse_export(source: bytes | str | Path) -> ParsedExport:
    """source = byte del file (come arrivano dall'API), percorso del file oppure testo XML."""
    if isinstance(source, Path) or (isinstance(source, str) and not source.lstrip().startswith("<")):
        source = Path(source).read_bytes()
    root = ET.fromstring(source)          # con i byte UTF-16 la codifica si legge dal BOM
    if root.tag != "biotronik-ieee11073-export":
        raise ValueError(f"Root inattesa: {root.tag}")
    dataset = root.find("dataset")
    if dataset is None:
        raise ValueError("Elemento <dataset> mancante")

    export = ParsedExport(format_version=root.get("format-version"), creator=root.get("creator"),
                          creator_version=root.get("creator-version"),
                          signed=root.find(XML_SIGNATURE) is not None)
    walk(dataset, "", export.values)
    return export
