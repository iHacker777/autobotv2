from __future__ import annotations
import asyncio
import threading
import logging
from typing import Optional
from telegram.constants import ParseMode
from telegram import Bot

logger = logging.getLogger(__name__)

class Messenger:
    """
    Thread-safe Telegram sender with 60s batching (when debug=False).(Need to remove the 60s batching later as it is a nuisance)
    Critical events bypass batching but image sending is not working in batch mode. Have to check later.
    """
    def __init__(self, *, bot: Bot, chat_id: int, loop: asyncio.AbstractEventLoop, debug: bool = True):
        self.bot = bot
        self.chat_id = chat_id
        self.loop = loop
        self.debug = debug

        self._buf: list[str] = []
        self._buf_lock = threading.Lock()
        self._flush_timer: Optional[asyncio.Handle] = None
        self._closed = False

    def set_debug(self, on: bool) -> None:
        self.debug = on

    # —— internal ——
    def _schedule_flush(self) -> None:
        if self._closed:
            return
        if self._flush_timer and not self._flush_timer.cancelled() and not self._flush_timer.when() < 0:
            # already scheduled
            return
        # schedule a one-shot callback that will run in loop thread
        self._flush_timer = self.loop.call_later(60.0, self._flush_now_threadsafe)

    def _flush_now_threadsafe(self) -> None:
        with self._buf_lock:
            if not self._buf:
                return
            text = "🧾 Summary (last 1 min):\n" + "\n".join(self._buf)
            self._buf.clear()
        fut = asyncio.run_coroutine_threadsafe(
            self.bot.send_message(self.chat_id, text, parse_mode=ParseMode.MARKDOWN),
            self.loop
        )
        # best-effort: log exceptions if any
        fut.add_done_callback(lambda f: f.exception() and logger.warning("Messenger flush error: %r", f.exception()))

    async def aclose(self) -> None:
        """Flush any pending messages and prevent new schedules (call during shutdown in loop context)."""
        self._closed = True
        if self._flush_timer and not self._flush_timer.cancelled():
            self._flush_timer.cancel()
        # final flush
        self._flush_now_threadsafe()

    # —— public (thread-safe) ——
    def send_event(self, text: str, kind: str = "INFO"):
        is_critical = kind in {"ERROR", "START", "STOP", "CAPTCHA", "OTP", "UPLOAD_OK"}
        if self.debug or is_critical:
            asyncio.run_coroutine_threadsafe(
                self.bot.send_message(self.chat_id, text, parse_mode=ParseMode.MARKDOWN),
                self.loop
            )
            return
        with self._buf_lock:
            self._buf.append(text)
        self._schedule_flush()

    def send_photo(self, photo, caption: str | None = None, kind: str = "INFO"):
        if self.debug or kind == "ERROR" or (caption and "captcha" in caption.lower()):
            asyncio.run_coroutine_threadsafe(
                self.bot.send_photo(self.chat_id, photo=photo, caption=caption),
                self.loop
            )
            return
        # non-critical photos are dropped by design but even critical photos may be dropped if debug=False. Have to check later.
