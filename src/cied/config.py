"""Impostazioni lette dal file .env (o dalle variabili d'ambiente)."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


@dataclass(frozen=True)
class Settings:
    # database
    db_conninfo: str = (f"host={env('DB_HOST', '127.0.0.1')} port={env('DB_PORT', '5432')} "
                        f"dbname={env('DB_NAME', 'cied')} user={env('DB_USER', 'cied')} "
                        f"password={env('DB_PASSWORD')}")
    # chiave segreta per gli pseudonimi
    pseudo_key: str = env("PSEUDO_KEY")

    # API BIOTRONIK Home Monitoring
    hm_base_url: str = env("HM_BASE_URL", "https://api.biotronik-homemonitoring.com/rest/api")
    hm_client_id: str = env("HM_CLIENT_ID")
    hm_client_secret: str = env("HM_CLIENT_SECRET")
    hm_cert_pem: str = env("HM_CERT_PEM", "secrets/cert.pem")
    hm_key_pem: str = env("HM_KEY_PEM", "secrets/private.key")
    hm_max_req_per_min: int = int(env("HM_MAX_REQ_PER_MIN", "4"))
    # solo per il server finto: CA che ha firmato il suo certificato (vuoto = API reale)
    hm_ca_bundle: str = env("HM_CA_BUNDLE")


settings = Settings()
