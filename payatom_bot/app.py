#app.py
from __future__ import annotations
import asyncio
import logging

from telegram.ext import ApplicationBuilder, Application

from .config import load_settings
from .handlers.core import register_core_handlers
from .handlers.aliases import register_alias_handlers
from .handlers.sessions import register_session_handlers
from .handlers.reports import register_report_handlers
from .messaging import Messenger
from .creds import load_creds  # <-- NEW
from .handlers.captcha import register_captcha_handlers

def build_application() -> Application:
    settings = load_settings()
    app = ApplicationBuilder().token(settings.telegram_token).build()

    # Registering handlers here but also registered it in sessions.py but doesn't work without this. Have to check properly later. 
    # Also not working without passsing settings here, have to check later.
    register_core_handlers(app, settings)
    register_alias_handlers(app)
    register_session_handlers(app, settings)
    register_report_handlers(app, settings)
    register_captcha_handlers(app)

    # Attach shared objects
    loop = asyncio.get_event_loop()
    app.bot_data["messenger"] = Messenger(bot=app.bot, chat_id=settings.telegram_chat_id, loop=loop, debug=True)
    app.bot_data["settings"] = settings

    # Load credentials once; access via: context.application.bot_data["creds_by_alias"][alias]
    app.bot_data["creds_by_alias"] = load_creds(settings.credentials_csv)
    app.bot_data["workers"] = {}  # <— add this line
    return app

def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = build_application()
    app.run_polling()

if __name__ == "__main__":
    main()
