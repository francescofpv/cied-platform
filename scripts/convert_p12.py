r"""Converte il certificato .p12 di BIOTRONIK nei due file PEM usati dal client API.

Sostituisce i comandi OpenSSL della documentazione (sez. 5.2.1):
    openssl pkcs12 -in cert.p12 -clcerts -nokeys -out cert.pem
    openssl pkcs12 -in cert.p12 -nocerts -nodes  -out private.key

Uso (dalla cartella del progetto, con .venv attivo):
    python scripts\convert_p12.py C:\percorso\XXXXXXXXX-cert.p12
    python scripts\convert_p12.py cert.p12 --out secrets --password LA_PASSWORD

Se --password non è indicata viene chiesta a schermo (senza mostrarla).
La password è quella mostrata quando l'amministratore ha creato l'export client.

Crea:
    secrets/cert.pem       certificato del client (+ eventuali certificati intermedi)
    secrets/private.key    chiave privata NON cifrata: non va condivisa né messa in git
"""
import argparse
import getpass
import os
import sys
from datetime import timezone
from pathlib import Path

from cryptography.hazmat.primitives.serialization import (Encoding, NoEncryption, PrivateFormat,
                                                          pkcs12)

ap = argparse.ArgumentParser(description="Converte il .p12 BIOTRONIK in cert.pem + private.key")
ap.add_argument("p12", help="file .p12 ricevuto da BIOTRONIK")
ap.add_argument("--out", default="secrets", help="cartella di destinazione (default: secrets)")
ap.add_argument("--password", help="password del .p12 (se manca viene chiesta)")
args = ap.parse_args()

p12_file = Path(args.p12)
if not p12_file.is_file():
    sys.exit(f"File non trovato: {p12_file}")

password = args.password if args.password is not None else getpass.getpass("Password del file .p12: ")

# 1) apertura del .p12
try:
    key, cert, chain = pkcs12.load_key_and_certificates(p12_file.read_bytes(), password.encode())
except ValueError:
    sys.exit("Password errata, oppure il file non è un .p12 valido.")
if key is None or cert is None:
    sys.exit("Il file .p12 non contiene sia il certificato sia la chiave privata.")

# 2) scrittura dei due file PEM
out = Path(args.out)
out.mkdir(parents=True, exist_ok=True)
cert_file, key_file = out / "cert.pem", out / "private.key"

cert_file.write_bytes(cert.public_bytes(Encoding.PEM)
                      + b"".join(c.public_bytes(Encoding.PEM) for c in chain or []))
key_file.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
os.chmod(key_file, 0o600)        # solo il proprietario può leggerla (su Windows ha effetto limitato)

# 3) riepilogo
expires = cert.not_valid_after_utc.astimezone(timezone.utc)
print(f"Certificato: {cert_file}")
print(f"Chiave privata: {key_file}")
print(f"Intestatario: {cert.subject.rfc4514_string()}")
print(f"Scadenza: {expires:%d/%m/%Y} (poi va richiesto un nuovo export client)")
print("Nel file .env:")
print(f"  HM_CERT_PEM={cert_file}")
print(f"  HM_KEY_PEM={key_file}")
