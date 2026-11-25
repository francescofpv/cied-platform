r"""Esporta in CSV le viste (o tabelle) pseudonimizzate dello schema cied.

Uso:
  python scripts\export_csv.py                          tutte le viste in export\
  python scripts\export_csv.py v_measurement v_implant  solo quelle indicate
  python scripts\export_csv.py --gruppo "Studio FA"     solo i pazienti del gruppo "Studio FA"
  python scripts\export_csv.py --gruppi                 elenca i gruppi di pazienti
  python scripts\export_csv.py --excel                  separatore ";" per Excel in italiano
  python scripts\export_csv.py --list                   elenca ciò che si può esportare
  python scripts\export_csv.py --out C:\cartella        cartella di destinazione

Con --gruppo, se non si indica --out, i file vanno in export\gruppo_<nome del gruppo>\.
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cied.db import connect  # noqa: E402
from cied.export import export_csv, exportable, groups  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("names", nargs="*", help="viste o tabelle di cied (default: tutte le viste v_*)")
ap.add_argument("--out", help="cartella di destinazione (default: export)")
ap.add_argument("--gruppo", help="esporta solo i pazienti di questo gruppo (nome del gruppo)")
ap.add_argument("--gruppi", action="store_true", help="elenca i gruppi di pazienti")
ap.add_argument("--excel", action="store_true", help='separatore ";" e BOM per Excel')
ap.add_argument("--list", action="store_true", help="elenca tabelle e viste esportabili")
args = ap.parse_args()

with connect() as conn:
    available = exportable(conn)
    if args.list:
        print("\n".join(available))
        sys.exit()
    all_groups = groups(conn)
    if args.gruppi:
        for name, n in all_groups:
            print(f"{name}  ({n} pazienti)")
        sys.exit() if all_groups else sys.exit("Nessun gruppo di pazienti.")
    if args.gruppo is not None:
        members = dict(all_groups).get(args.gruppo)
        if members is None:
            sys.exit(f"Gruppo '{args.gruppo}' inesistente. Gruppi disponibili: "
                     + (", ".join(g for g, _ in all_groups) or "nessuno"))
        print(f"Gruppo '{args.gruppo}': {members} pazienti")
    if args.out:
        out = Path(args.out)
    elif args.gruppo is not None:
        out = Path("export") / ("gruppo_" + re.sub(r"[^\w-]+", "_", args.gruppo).strip("_"))
    else:
        out = Path("export")
    out.mkdir(parents=True, exist_ok=True)
    for name in args.names or [n for n in available if n.startswith("v_")]:
        try:
            rows = export_csv(conn, name, out / f"{name}.csv", args.excel, group=args.gruppo)
        except ValueError as e:
            print(f"{name}: ERRORE {e}")
            continue
        print(f"{name}: {rows} righe -> {out / f'{name}.csv'}")
