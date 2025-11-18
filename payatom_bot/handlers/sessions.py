# payatom_bot/sessions.py
from __future__ import annotations
import re
import os
import inspect
from datetime import datetime, timedelta  # 🔹 UPDATED
from typing import Dict, Optional, List, Any, Tuple

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import asyncio
from datetime import datetime, timedelta
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..config import Settings
from ..captcha_solver import TwoCaptcha

# Workers
from ..workers.tmb import TMBWorker
from ..workers.iob import IOBWorker          # used for both IOB retail & corporate 
from ..workers.kgb import KGBWorker
from ..workers.idbi import IDBIWorker
from ..workers.idfc import IDFCWorker
from ..workers.canara import CanaraWorker

# =========================
# Internal registry helpers related to workers.
# =========================
def _get_registry(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, object]:
    reg = context.application.bot_data.get("workers")
    if reg is None:
        reg = {}
        context.application.bot_data["workers"] = reg
    return reg


def _normalize_bank_label(label: str) -> str:
    if not label:
        return ""
    lbl = label.strip().upper().replace("&", "AND")
    return " ".join(lbl.split())


WORKER_BY_BANK: Dict[str, Any] = {
    "TMB": TMBWorker,
    "IOB": IOBWorker,
    "IOB CORPORATE": IOBWorker,  # same class handles both flows
    "KGB": KGBWorker,
    "KERALA GRAMIN BANK": KGBWorker,  # need exact label
    "IDBI": IDBIWorker,
    "IDFC": IDFCWorker,
    "CANARA": CanaraWorker,    
}

_ALIASES = {
    "INDIAN OVERSEAS BANK": "IOB",
    "IOB CORP": "IOB CORPORATE",
    "IOB-CORPORATE": "IOB CORPORATE",
    "CNRB": "CANARA",
}


def _pick_worker_class(bank_label: str):
    lbl = _normalize_bank_label(bank_label)
    lbl = _ALIASES.get(lbl, lbl)
    return WORKER_BY_BANK.get(lbl), lbl


def _instantiate_worker(worker_cls, common_kwargs: Dict[str, Any]):
    """Pass only kwargs that the class actually accepts."""
    sig = inspect.signature(worker_cls.__init__)
    allowed = {k: v for k, v in common_kwargs.items() if k in sig.parameters}
    return worker_cls(**allowed)


# =========================
# Date-range parsing (KGB)
# =========================
def _parse_date(s: str) -> datetime:
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Invalid date: {s} (expected dd/mm/yyyy)")


def _extract_aliases_and_range(args: List[str]) -> Tuple[List[str], Optional[Tuple[datetime, datetime]]]:
    """
    Supports:
      /run a1 a2
      /run a1_kgb from dd/mm/yyyy to dd/mm/yyyy (this is only working with KGB, have to implement for all banks later)
      /run a1 a2 from dd/mm/yy to dd/mm/yy  (range applied only to KGB workers - need to fix this later :( ))
    """
    if not args:
        return [], None
    # find 'from' and 'to'
    try:
        from_idx = next(i for i, t in enumerate(args) if t.lower() == "from")
    except StopIteration:
        # no range → all are aliases
        return args, None

    aliases = [t for t in args[:from_idx] if t.lower() not in {"from", "to"}]
    # parse range tokens
    try:
        to_idx = next(i for i, t in enumerate(args[from_idx + 1 :], start=from_idx + 1) if t.lower() == "to")
    except StopIteration:
        raise ValueError("Missing `to` in date range. Use: /run <alias> from dd/mm/yyyy to dd/mm/yyyy")

    if to_idx - from_idx != 2 or len(args) <= to_idx + 1:
        raise ValueError("Invalid range. Use: /run <alias> from dd/mm/yyyy to dd/mm/yyyy")

    dt_from = _parse_date(args[from_idx + 1])
    dt_to = _parse_date(args[to_idx + 1])
    if dt_to < dt_from:
        raise ValueError("`to` date is before `from` date.")

    return aliases, (dt_from, dt_to)


# --- helper used for masking account no. It's not really necessary now but in future may be safer. ---
def _mask_acct(acct: str | int | None) -> str:
    s = (str(acct or "").strip())
    return ("****" + s[-4:]) if len(s) >= 4 else s or "—"


# NEW!! helper to format "time elapsed"
def _format_ago(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    if total < 0:
        total = 0
    mins, secs = divmod(total, 60)
    hours, mins = divmod(mins, 60)
    parts: List[str] = []
    if hours:
        parts.append(f"{hours}h")
    if mins:
        parts.append(f"{mins}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


# =====================================
# Starter with optional KGB date range
# =====================================
def start_worker_for_alias(
    context: ContextTypes.DEFAULT_TYPE,
    alias: str,
    date_range: Optional[Tuple[datetime, datetime]] = None,
) -> str:
    """
    Starts the correct worker for <alias>.
    If date_range is provided, it is **applied only to KGBWorker** (from_dt/to_dt set before start, also I have to implement the same variables for other banks so that I can easily set the dates from runtime itself. Removed the default option that we used to have as it is fancy but pretty much useless).
    """
    app = context.application
    settings: Settings = app.bot_data["settings"]
    messenger = app.bot_data["messenger"]
    creds_by_alias: Dict[str, dict] = app.bot_data.get("creds_by_alias", {})

    alias = alias.strip()
    if not alias:
        return "❌ Alias was empty."

    cred = creds_by_alias.get(alias)
    if not cred:
        return f"❌ Unknown alias `{alias}`. Check your CSV."

    workers = _get_registry(context)
    existing = workers.get(alias)
    if existing and getattr(existing, "is_alive", lambda: False)():
        return f"⚠️ `{alias}` is already running."

    worker_cls, norm_bank = _pick_worker_class(cred.get("bank_label", ""))
    if not worker_cls:
        return f"⏸️ `{alias}` uses unsupported bank `{cred.get('bank_label','')}`."

    # Build per-alias Chrome profile dir to avoid profile locks
    profile_dir = os.path.join(settings.profile_root, alias)

    common_kwargs = dict(
        bot=app.bot,
        chat_id=settings.telegram_chat_id,
        alias=alias,
        cred=cred,
        messenger=messenger,
        profile_dir=profile_dir,
        two_captcha=TwoCaptcha(settings.two_captcha_key),
    )

    try:
        worker = _instantiate_worker(worker_cls, common_kwargs)
        # Apply custom dates ONLY for KGB but I can implement for other banks later. Mostly make this global and make other bakns check this too.
        if isinstance(worker, KGBWorker) and date_range:
            worker.from_dt, worker.to_dt = date_range  # attributes read by KGBWorker
        worker.start()
        workers[alias] = worker
    except Exception as e:
        return f"❌ Failed to start `{alias}` ({norm_bank}): `{e}`"

    if isinstance(worker, KGBWorker) and date_range:
        f, t = date_range
        return f"✅ Started `{alias}` ({norm_bank}) with custom range {f:%d/%m/%Y} → {t:%d/%m/%Y}."
    return f"✅ Started `{alias}` ({norm_bank})."


# ======================
# Telegram bot handlers
# ======================

# Predefined Telegram group chat IDs (replace with actual chat IDs)
ALERT_GROUPS = [
    -1002881121333,  # Replace with actual Telegram group chat ID
    -1002792849989,  # Replace with another group chat ID
]

# Define balance thresholds and corresponding emojis
THRESHOLDS = {
    50000: "⚠️ Alert",
    60000: "🟠 Alert",
    70000: "🟡 Urgent!\nPlease transfer funds",
    90000: "🚨 High Urgency\nTransfer funds immediately",
    100000: "🚨🚨 CRITICAL EMERGENCY ALERT!\nStop account and transfer funds immediately"
}

# Function to check balance and send alert
async def send_balance_alert(context, message: str):
    """
    Function to send balance alerts to predefined Telegram groups
    """
    for group in ALERT_GROUPS:
        try:
            await context.bot.send_message(
                chat_id=group,
                text=message,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            print(f"Error sending message to group {group}: {e}")

async def balance_check_alert(context: ContextTypes.DEFAULT_TYPE):
    workers = _get_registry(context)
    creds_by_alias: Dict[str, dict] = context.application.bot_data.get("creds_by_alias", {})
    
    now = datetime.now()

    for alias, worker in workers.items():
        if not getattr(worker, "is_alive", lambda: False)():
            continue

        # Get balance info from worker
        balance = getattr(worker, "last_balance", None)
        if balance is None:
            continue  # No balance, skip

        # Parse the balance to float for comparison
        try:
            balance_float = float(balance.replace(",", "").replace("₹", "").strip())
        except ValueError:
            continue  # Skip if balance is invalid

        # Check if the balance exceeds any of the thresholds
        for threshold, urgency in sorted(THRESHOLDS.items(), reverse=True):
            if balance_float >= threshold:
                # Send alert if balance exceeds the threshold
                message = (
                    f"🚨 *Balance Alert* 🚨\n"
                    f"Alias: `{alias}`\n"
                    f"Current Balance: 💰{balance_float:.2f}\n"
                    f"Threshold Exceeded: 💰{threshold:.2f} - {urgency}\n"
                    f"Timestamp: {now.strftime('%Y-%m-%d %H:%M:%S')}"
                )
                await send_balance_alert(context, message)

                break  # Only send one alert per threshold crossing



# Function to start the periodic balance check every 3 minutes
async def start_balance_check(context: ContextTypes.DEFAULT_TYPE):
    while True:
        await balance_check_alert(context)
        await asyncio.sleep(180)  # Wait for 3 minutes before checking again


# In your `sessions.py`, where you initialize your bot and workers, start this task
# You can use a command or schedule it directly

async def start_balance_monitoring(update, context):
    # Start the periodic check in the background
    asyncio.create_task(start_balance_check(context))
    await update.message.reply_text("Started monitoring balance and sending alerts.")


async def run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /run <alias>...
    /run <alias> from dd/mm/yyyy to dd/mm/yyyy   (range applies to KGB only)
    """
    try:
        aliases, dr = _extract_aliases_and_range(context.args)
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}", parse_mode="Markdown")
        return

    if not aliases:
        await update.message.reply_text(
            "Usage: `/run <alias>` or `/run <alias> from dd/mm/yyyy to dd/mm/yyyy`",
            parse_mode="Markdown",
        )
        return

    lines: List[str] = []
    for alias in aliases:
        msg = start_worker_for_alias(context, alias, date_range=dr)
        lines.append(msg)

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /stop <alias> [<alias2> ...]
    """
    if not context.args:
        await update.message.reply_text("Usage: `/stop <alias>` or `/stop <a1> <a2>`", parse_mode="Markdown")
        return

    workers = _get_registry(context)
    lines: List[str] = []
    for alias in context.args:
        alias = alias.strip()
        w = workers.get(alias)
        if not w or not getattr(w, "is_alive", lambda: False)():
            lines.append(f"ℹ️ `{alias}` is not running.")
            continue
        try:
            w.stop()
            w.join(timeout=5.0)
        except Exception:
            pass
        workers.pop(alias, None)
        lines.append(f"🛑 Stopped `{alias}`.")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def status_cmd(update, context):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /status <alias>")
        return
    alias = args[0]
    w = _get_registry(context).get(alias)
    if not w:
        await update.message.reply_text(f"`{alias}` is not running.")
        return
    # uses BaseWorker.screenshot_all_tabs
    w.screenshot_all_tabs(f"Status requested for {alias}")
    await update.message.reply_text(f"📸 Captured status for `{alias}`")


async def stopall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /stopall
    """
    workers = _get_registry(context)
    stopped = []
    for alias, w in list(workers.items()):
        try:
            if getattr(w, "is_alive", lambda: False)():
                w.stop()
                w.join(timeout=5.0)
            stopped.append(alias)
        except Exception:
            pass
        finally:
            workers.pop(alias, None)
    if not stopped:
        await update.message.reply_text("ℹ️ No workers were running.", parse_mode="Markdown")
    else:
        await update.message.reply_text("🧹 Stopped: " + ", ".join(f"`{a}`" for a in stopped), parse_mode="Markdown")


# 🔹 NEW: smarter /active (keeps /running unchanged)
async def active_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /active

    Similar to /running, but also checks AutoBank uploads:
      • For each running alias, show last upload time (HH:MM:SS) and how long ago.
      • If no upload ever, or last upload > 5 minutes ago → show as error.
    """
    workers = _get_registry(context)
    # Only live workers
    running = {
        alias: w
        for alias, w in workers.items()
        if getattr(w, "is_alive", lambda: False)()
    }
    if not running:
        await update.message.reply_text("😴 No active workers.", parse_mode="Markdown")
        return

    creds_by_alias: Dict[str, dict] = context.application.bot_data.get("creds_by_alias", {})
    now = datetime.now()
    threshold = timedelta(minutes=5)

    ok_lines: List[str] = []
    bad_lines: List[str] = []

    for alias, w in running.items():
        bank = _normalize_bank_label(creds_by_alias.get(alias, {}).get("bank_label", ""))
        label = f"`{alias}`" + (f" ({bank})" if bank else "")

        last_upload = getattr(w, "last_upload_at", None)
        if not last_upload:
            bad_lines.append(f"❌ {label} — no AutoBank upload recorded yet.")
            continue

        age = now - last_upload
        ago_str = _format_ago(age)
        last_str = last_upload.strftime("%H:%M:%S")

        if age > threshold:
            bad_lines.append(
                f"⛔ {label} — last AutoBank upload at *{last_str}* ({ago_str} ago; > 5 min)."
            )
        else:
            ok_lines.append(
                f"🟢 {label} — last AutoBank upload at *{last_str}* ({ago_str} ago)."
            )

    lines: List[str] = []
    if ok_lines:
        lines.append("✅ Active workers with recent AutoBank uploads:")
        lines.extend(ok_lines)
    if bad_lines:
        if lines:
            lines.append("")  # blank line separator
        lines.append("⚠️ Issues detected (no upload / older than 5 min):")
        lines.extend(bad_lines)

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /balance                -> show balances for all running workers
    /balance a1 a2          -> show balances only for given aliases
    """
    workers = _get_registry(context)
    creds_by_alias: Dict[str, dict] = context.application.bot_data.get("creds_by_alias", {})

    # Pick targets: given args or all running
    targets = context.args if context.args else list(workers.keys())
    if not targets:
        await update.message.reply_text("😴 No active workers.", parse_mode="Markdown")
        return

    lines: list[str] = []
    for alias in targets:
        w = workers.get(alias)
        if not w or not getattr(w, "is_alive", lambda: False)():
            lines.append(f"`{alias}` — not running")
            continue

        cred = creds_by_alias.get(alias, {})
        bank = cred.get("bank_label", "?")
        acct = _mask_acct(cred.get("account_number"))
        bal = getattr(w, "last_balance", None) or "loading..."

        # Correct Markdown formatting: safely display balance and account number
        # Escape special characters that may interfere with Markdown formatting
        safe_alias = alias.replace("_", r"\_").replace("*", r"\*")
        safe_balance = bal.replace("_", r"\_").replace("*", r"\*")
        safe_account = acct.replace("_", r"\_").replace("*", r"\*")

        lines.append(f"**{safe_alias}** | 💰**{safe_balance}**")

    # Send the formatted response
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def running_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /running
    """
    workers = _get_registry(context)
    active = [a for a, w in workers.items() if getattr(w, "is_alive", lambda: False)()]
    if not active:
        await update.message.reply_text("😴 No active workers.", parse_mode="Markdown")
        return

    creds_by_alias: Dict[str, dict] = context.application.bot_data.get("creds_by_alias", {})
    decorated = []
    for a in active:
        bank = _normalize_bank_label(creds_by_alias.get(a, {}).get("bank_label", ""))
        decorated.append(f"`{a}` ({bank})" if bank else f"`{a}`")

    await update.message.reply_text("🟢 Running: " + ", ".join(decorated), parse_mode="Markdown")

async def start_monitoring(update, context):
    await start_balance_monitoring(update, context)


def register_session_handlers(app: Application, settings: Settings) -> None:
    """
    Always register session management handlers here. Do not fck this up AGAIN.
    Also may need /reports handler later but right now will leave it as it is, but have to comment the code later
    """
    app.add_handler(CommandHandler("run", run_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("stopall", stopall_cmd))
    app.add_handler(CommandHandler("running", running_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("active", active_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("start_monitoring", start_monitoring))