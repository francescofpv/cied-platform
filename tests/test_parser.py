"""Test del parser su un XML sintetico (nessun dato reale nel repository)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import pytest  # noqa: E402
from defusedxml import ElementTree as ET  # noqa: E402

from cied.parser import parse_export  # noqa: E402
from cied.parser.ieee11073 import xml_text  # noqa: E402

SAMPLE = """<?xml version="1.0" encoding="UTF-16"?>
<biotronik-ieee11073-export creator="BioHMSC" creator-version="3.68.0" format-version="4.10">
 <dataset><section name="MDC"><section name="IDC">
  <section name="DEV">
   <value code="720898" name="MODEL" type="String">TEST DEVICE</value>
   <value code="720899" name="SERIAL" type="String">00000001</value>
   <value name="NOTE" type="String">Delùca è già</value>
  </section>
  <section name="SESS">
   <value code="721025" name="DTM" type="DateTime">20261023T022001+0200</value>
  </section>
  <section name="STAT">
   <section name="EPISODE"><value code="738032" name="TOTAL_COUNT" type="Numeric">5</value></section>
   <section name="EPISODE"><value code="738032" name="TOTAL_COUNT" type="Numeric">7</value></section>
   <section name="AT"><value code="737696" name="BURDEN_PERCENT" type="Numeric" unit="%">12</value></section>
  </section>
 </section></section></dataset>
</biotronik-ieee11073-export>"""


def _check(exp):
    assert exp.format_version == "4.10"
    assert exp.get("MDC/IDC/DEV/SERIAL").raw == "00000001"
    assert exp.get("MDC/IDC/DEV/NOTE").raw == "Delùca è già"      # accenti decodificati bene
    assert exp.get("MDC/IDC/SESS/DTM").tz_known is True
    assert exp.get("MDC/IDC/STAT/EPISODE[2]/TOTAL_COUNT").num == 7
    burden = exp.get("MDC/IDC/STAT/AT/BURDEN_PERCENT")
    assert burden.code == 737696 and burden.unit == "%"


# Gli export originali: UTF-16 con BOM (il Service Center usa big endian).
ORIGINALS = {
    "originale Service Center: UTF-16 big endian con BOM": b"\xfe\xff" + SAMPLE.encode("utf-16-be"),
    "UTF-16 little endian con BOM": b"\xff\xfe" + SAMPLE.encode("utf-16-le"),
}
# Copie risalvate da altri programmi: vanno rifiutate.
COPIES = {
    "copia risalvata in UTF-8 (dichiarazione UTF-16 rimasta)": SAMPLE.encode("utf-8"),
    "copia UTF-8 con BOM": b"\xef\xbb\xbf" + SAMPLE.encode("utf-8"),
}


@pytest.mark.parametrize("data", ORIGINALS.values(), ids=ORIGINALS.keys())
def test_originals(data):
    _check(parse_export(data))
    assert xml_text(data) == SAMPLE


@pytest.mark.parametrize("data", COPIES.values(), ids=COPIES.keys())
def test_copies_are_rejected(data):
    with pytest.raises(ET.ParseError, match="encoding"):
        parse_export(data)
    with pytest.raises(ValueError, match="UTF-16 con BOM"):
        xml_text(data)


def test_text_and_path_input(tmp_path):
    _check(parse_export(SAMPLE))                       # testo già decodificato
    f = tmp_path / "export.xml"
    f.write_bytes(b"\xfe\xff" + SAMPLE.encode("utf-16-be"))
    _check(parse_export(f))                            # Path
    _check(parse_export(str(f)))                       # percorso come stringa
