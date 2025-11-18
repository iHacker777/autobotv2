from __future__ import annotations
import os
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

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
    """
    Load application settings from environment variables.
    
    Raises:
        RuntimeError: If required environment variables are missing or invalid
    """
    # Try to load from .env file (development convenience)
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv()
        logger.info("Loaded environment variables from .env file")
    except ImportError:
        logger.debug("python-dotenv not installed; skipping .env file")
    except Exception as e:
        logger.warning("Failed to load .env file: %s", e)

    # Validate required token
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token or not token.strip():
        raise RuntimeError(
            "❌ TELEGRAM_TOKEN is required in environment or .env file.\n"
            "Please set it before starting the bot."
        )

    # Validate and parse chat ID
    chat_id_raw = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not chat_id_raw:
        raise RuntimeError(
            "❌ TELEGRAM_CHAT_ID is required in environment or .env file.\n"
            "Please set it before starting the bot."
        )
    
    try:
        chat_id = int(chat_id_raw)
    except ValueError as e:
        raise RuntimeError(
            f"❌ TELEGRAM_CHAT_ID must be a valid integer, got: {chat_id_raw!r}"
        ) from e

    # Optional settings with defaults
    two_captcha = os.environ.get("TWO_CAPTCHA_API_KEY", "")
    if not two_captcha:
        logger.warning(
            "TWO_CAPTCHA_API_KEY not set; CAPTCHA solving will require manual input"
        )

    autobank_url = os.environ.get(
        "AUTOBANK_UPLOAD_URL",
        "https://autobank.payatom.in/bankupload.php"
    )
    
    creds_csv = os.environ.get("CREDENTIALS_CSV", "tmb_credentials.csv")
    
    # Validate credentials file exists
    if not os.path.exists(creds_csv):
        logger.warning(
            "Credentials CSV file not found at: %s\n"
            "Make sure to create it before running workers.",
            creds_csv
        )

    logger.info(
        "Settings loaded successfully:\n"
        "  - Chat ID: %s\n"
        "  - Credentials CSV: %s\n"
        "  - AutoBank URL: %s\n"
        "  - 2Captcha: %s",
        chat_id,
        creds_csv,
        autobank_url,
        "configured" if two_captcha else "not configured"
    )

    return Settings(
        telegram_token=token,
        telegram_chat_id=chat_id,
        credentials_csv=creds_csv,
        two_captcha_key=two_captcha,
        autobank_upload_url=autobank_url,
    )
