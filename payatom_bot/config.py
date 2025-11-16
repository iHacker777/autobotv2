from __future__ import annotations
import os
from dataclasses import dataclass

@dataclass(frozen=True)
class Settings:
    telegram_token: str
    telegram_chat_id: int
    credentials_csv: str
    two_captcha_key: str
    autobank_upload_url: str
    max_profiles: int = 10
    profile_root: str = os.path.join(os.path.expanduser("~"), "chrome-profiles")

def load_settings() -> Settings:
    # Optional local .env support (dev convenience)
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv()
    except Exception:
        pass

    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN is required in environment or .env")

    chat_id_raw = os.environ.get("TELEGRAM_CHAT_ID", "0")
    try:
        chat_id = int(chat_id_raw)
    except ValueError as e:
        raise RuntimeError("TELEGRAM_CHAT_ID must be an integer") from e

    two_captcha = os.environ.get("TWO_CAPTCHA_API_KEY", "")
    autobank_url = os.environ.get("AUTOBANK_UPLOAD_URL", "https://autobank.payatom.in/bankupload.php")
    creds_csv = os.environ.get("CREDENTIALS_CSV", "tmb_credentials.csv")

    return Settings(
        telegram_token=token,
        telegram_chat_id=chat_id,
        credentials_csv=creds_csv,
        two_captcha_key=two_captcha,
        autobank_upload_url=autobank_url,
    )
