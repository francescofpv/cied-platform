"""Pseudonimi stabili calcolati con HMAC-SHA256 e chiave segreta (PSEUDO_KEY).

Lo stesso input con la stessa chiave dà sempre lo stesso pseudonimo, quindi
un dispositivo o un paziente vengono riconosciuti a ogni nuovo export senza
salvarne l'identità nello schema pseudonimizzato.
"""
import hashlib
import hmac


def _hmac(key: str, msg: str) -> str:
    if not key:
        raise RuntimeError("PSEUDO_KEY mancante nel file .env")
    return hmac.new(key.encode(), msg.encode(), hashlib.sha256).hexdigest()[:20]


def device_pseudonym(key: str, manufacturer: str, model: str | None, serial: str) -> str:
    """Il dispositivo è identificato da produttore + modello + seriale (il seriale da solo non basta)."""
    return _hmac(key, f"DEV:{manufacturer}:{model or ''}:{serial}")


def patient_pseudonym(key: str, patient_no: int) -> str:
    """Calcolato sul codice paziente interno, che non cambia anche se cambiano gli ID esterni."""
    return _hmac(key, f"PT:{patient_no}")
