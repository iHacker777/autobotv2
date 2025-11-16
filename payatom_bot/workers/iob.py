# payatom_bot/workers/iob.py
from __future__ import annotations

import os
import re
import time
from io import BytesIO
from datetime import datetime, timedelta
from typing import Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, ElementClickInterceptedException

from ..worker_base import BaseWorker
from ..captcha_solver import TwoCaptcha
from ..autobank_client import AutoBankClient


class IOBWorker(BaseWorker):
    """
    Indian Overseas Bank (Retail + Corporate).

    Decides 'personal' vs 'corporate' by alias suffix or bank_label from creds.

    CSV schema used:
        alias,login_id,user_id,username,password,account_number

    cred fields available (via creds loader):
        cred["auth_id"]         # canonical user (username/login_id/user_id)
        cred["username"]        # raw
        cred["login_id"]        # raw (corp)
        cred["user_id"]         # raw (corp)
        cred["password"]
        cred["account_number"]
        cred["bank_label"]      # "IOB" OR "IOB CORPORATE" (derived from alias)
    """

    LOGIN_URL = "https://netbanking.iob.bank.in/ibanking/html/index.html"

    def __init__(
        self,
        *,
        bot,
        chat_id: int,
        alias: str,
        cred: dict,
        messenger,
        profile_dir: str,
        two_captcha: Optional[TwoCaptcha] = None,
    ):
        super().__init__(
            bot=bot,
            chat_id=chat_id,
            alias=alias,
            cred=cred,
            messenger=messenger,
            profile_dir=profile_dir,
        )
        self.wait = WebDriverWait(self.driver, 20)
        self.solver = two_captcha
        self.iob_win: Optional[str] = None
        self._captcha_id: Optional[str] = None
        self.captcha_code: Optional[str] = None

    # ------------------------------------------------------------------
    # Robust tab-cycling (override BaseWorker version)
    # ------------------------------------------------------------------
    def _cycle_tabs(self) -> None:
        """
        Close all existing tabs, open a fresh about:blank tab and reset
        login state – adapted from the legacy IOBWorker._retry().
        """
        d = self.driver
        try:
            handles_before = list(d.window_handles)
            if not handles_before:
                self.error("IOB: no windows to cycle; driver may already be closed.")
                return

            # 1) Open a new blank tab
            d.execute_script("window.open('about:blank','_blank');")
            time.sleep(0.5)

            # 2) Find the newly opened handle
            handles_after = list(d.window_handles)
            new_handle = None
            for h in handles_after:
                if h not in handles_before:
                    new_handle = h
                    break

            if not new_handle:
                self.error("IOB: failed to locate new tab during cycle.")
                return

            # 3) Close every old tab
            for h in handles_before:
                try:
                    d.switch_to.window(h)
                    d.close()
                except Exception:
                    pass

            # 4) Switch into the fresh tab and reset state
            d.switch_to.window(new_handle)
            self.iob_win = new_handle
            self.captcha_code = None
            self.logged_in = False
            self.info("🔄 IOB: cycled tabs – will re-login on next loop.")
        except Exception as e:
            # If tab cycling itself explodes, stop the worker cleanly
            self.error(f"IOB: tab cycling failed; stopping worker. {type(e).__name__}: {e}")
            try:
                self.screenshot_all_tabs("IOB tab-cycle failure")
            except Exception:
                pass
            self.stop_evt.set()
            try:
                d.quit()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Detect "You are Logged OUT..." page and react immediately
    # ------------------------------------------------------------------
    def _check_logged_out_and_cycle(self) -> None:
        """
        If the current page shows the IOB 'You are Logged OUT of internet banking
        due to ANY of the following reasons' message, immediately cycle tabs and
        force a retry by raising TimeoutException.
        """
        try:
            source = self.driver.page_source
        except Exception:
            return

        if "You are Logged OUT of internet banking due to ANY of the following reasons" in source:
            self.info("IOB: detected logged-out page; cycling tabs and retrying login.")
            self._cycle_tabs()
            # Raise so outer loop treats this as a failure and re-runs _login()
            raise TimeoutException("IOB logged out (server message)")

    # ———————————————————
    # Public thread entry
    # ———————————————————
    def run(self) -> None:
        self.info("🚀 Starting IOB automation")
        retry_count = 0
        try:
            while not self.stop_evt.is_set():
                try:
                    # Fresh login for each outer loop
                    self._login()
                    retry_count = 0  # reset after successful login

                    # Steady-state: download → upload → balance → sleep
                    while not self.stop_evt.is_set():
                        # Before doing anything, check if server has logged us out
                        self._check_logged_out_and_cycle()

                        # 1) Download + upload statement (with internal retries)
                        self._run_with_retries(self._download_and_upload_statement, "IOB statement")

                        # Check again before balance enquiry
                        self._check_logged_out_and_cycle()

                        # 2) Balance enquiry (best-effort; swallow layout/selector changes)
                        try:
                            self._balance_enquiry()
                        except TimeoutException:
                            # Logged-out path bubbles up
                            raise
                        except Exception:
                            self.info("Balance enquiry skipped (selector/layout change?)")

                        time.sleep(60)  # steady-state sleep
                except Exception as e:
                    retry_count += 1
                    self.error(f"IOB loop error: {e!r} — retry {retry_count}/5")
                    self.screenshot_all_tabs("IOB error")

                    if retry_count > 5:
                        self.error("❌ Too many failures. Stopping.")
                        return

                    # crude reset → blank tab; next loop iteration will re-login
                    self._cycle_tabs()
        finally:
            self.stop()

    # ———————————————————
    # Steps
    # ———————————————————
    def _login(self) -> None:
        d = self.driver
        w = self.wait

        # 1) Open login page
        d.get(self.LOGIN_URL)

        # 2) Click “Continue to Internet Banking Home Page”
        w.until(
            EC.element_to_be_clickable((By.LINK_TEXT, "Continue to Internet Banking Home Page"))
        ).click()

        # 3) Choose personal vs corporate
        is_corp = False
        bank_label = (self.cred.get("bank_label") or "").upper().strip()
        if bank_label == "IOB CORPORATE" or self.alias.lower().endswith("_iobcorp"):
            is_corp = True
        role_text = "Corporate Login" if is_corp else "Personal Login"
        w.until(EC.element_to_be_clickable((By.LINK_TEXT, role_text))).click()

        # 4) Fill credentials
        if is_corp:
            # Corporate uses separate loginId + userId + password
            d.find_element(By.NAME, "loginId").send_keys(self.cred.get("login_id", ""))
            d.find_element(By.NAME, "userId").send_keys(self.cred.get("user_id", ""))
            d.find_element(By.NAME, "password").send_keys(self.cred["password"])
        else:
            # Retail takes loginId + password (loginId is your "username")
            # We prefer the canonical auth_id if present; fallback to `username`
            user = self.cred.get("auth_id") or self.cred.get("username") or ""
            d.find_element(By.NAME, "loginId").send_keys(user)
            d.find_element(By.NAME, "password").send_keys(self.cred["password"])

        # 5) Captcha image – ensure it's fully in view before screenshot
        img = WebDriverWait(d, 10).until(
            EC.presence_of_element_located((By.ID, "captchaimg"))
        )
        # Scroll into view so Selenium doesn't crop at the viewport edge
        d.execute_script("arguments[0].scrollIntoView(true);", img)
        time.sleep(1)  # let layout/animations settle

        # Re-locate after scroll to get a fresh bounding box
        img = WebDriverWait(d, 10).until(
            EC.visibility_of_element_located((By.ID, "captchaimg"))
        )
        img_bytes = img.screenshot_as_png  # full element bytes

        # 6) Solve captcha
        self.info("🤖 Solving CAPTCHA via 2Captcha…")
        solution, cid = (None, None)
        if self.solver and self.solver.key:
            solution, cid = self.solver.solve(
                img_bytes,
                min_len=6,
                max_len=6,
                regsense=True,
            )

        if solution:
            # normalize: strip spaces and force uppercase (IOB is case-sensitive)
            normalized = re.sub(r"\s+", "", solution).upper()
            self.captcha_code, self._captcha_id = normalized, cid
            self.info(f"✅ Auto-solved: `{normalized}`")
        else:
            # Legacy behaviour: still send the CAPTCHA for manual solving
            self.msgr.send_photo(
                BytesIO(img_bytes),
                f"[{self.alias}] 🔐 Please solve captcha",
                kind="CAPTCHA",
            )
            # If you want manual solving, wire your Telegram handler to set self.captcha_code.
            raise TimeoutException("CAPTCHA not solved — add your manual flow if needed.")

        # 7) Fill the captcha (name is 'captchaid', not 'captchajid')
        field = d.find_element(By.NAME, "captchaid")
        field.clear()
        field.send_keys((self.captcha_code or "").strip().upper())

        # 8) Submit
        d.find_element(By.ID, "btnSubmit").click()

        # 8a) Wrong captcha? detect and report to 2Captcha
        try:
            err_span = WebDriverWait(d, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.otpmsg span.red"))
            )
            if (
                "captcha entered is incorrect" in (err_span.text or "").lower()
                and self._captcha_id
            ):
                self.error("❌ CAPTCHA wrong — reporting to 2Captcha and retrying…")
                try:
                    self.solver.report_bad(self._captcha_id)
                except Exception:
                    pass
                self._cycle_tabs()
                raise TimeoutException("Captcha incorrect")
        except TimeoutException:
            # no error shown → carry on to dashboard
            pass

        # 9) Wait for main nav (logged in)
        w.until(EC.presence_of_element_located((By.CSS_SELECTOR, "nav.accordian")))
        self.iob_win = d.current_window_handle
        self.logged_in = True
        self.info("✅ Logged in!")

    def _download_and_upload_statement(self) -> None:
        d = self.driver

        # 1) Navigate to “Account statement” (IOB is sometimes slow → use 60s wait)
        stmt_link = WebDriverWait(d, 60).until(
            EC.element_to_be_clickable((By.LINK_TEXT, "Account statement"))
        )
        d.execute_script("arguments[0].scrollIntoView({block:'center'});", stmt_link)
        time.sleep(0.3)
        try:
            stmt_link.click()
        except ElementClickInterceptedException:
            # fallback: JS click if some overlay/header is intercepting
            d.execute_script("arguments[0].click();", stmt_link)
        time.sleep(3)  # let menu animation + content load

        # 2) Pick the right account by prefix match
        acct_sel = self.wait.until(EC.element_to_be_clickable((By.ID, "accountNo")))
        dropdown = Select(acct_sel)
        acct_no = (self.cred.get("account_number") or "").strip()
        if acct_no:
            for opt in dropdown.options:
                if opt.text.strip().startswith(acct_no):
                    dropdown.select_by_visible_text(opt.text)
                    break

        # 3) Compute date window (keep your legacy logic)
        now = datetime.now()
        from_dt = now - timedelta(days=1) if now.hour < 6 else now
        to_dt = now
        from_str = from_dt.strftime("%m/%d/%Y")
        to_str = to_dt.strftime("%m/%d/%Y")

        # 4) Fill "From Date" (remove readonly and set via JS)
        from_input = self.wait.until(EC.presence_of_element_located((By.ID, "fromDate")))
        d.execute_script("arguments[0].removeAttribute('readonly')", from_input)
        d.execute_script("arguments[0].value = arguments[1]", from_input, from_str)

        # 5) Fill "To Date"
        to_input = self.wait.until(EC.presence_of_element_located((By.ID, "toDate")))
        d.execute_script("arguments[0].removeAttribute('readonly')", to_input)
        d.execute_script("arguments[0].value = arguments[1]", to_input, to_str)

        # 6) Click “View” (it's an <input> button)
        view_btn = self.wait.until(
            EC.element_to_be_clickable((By.ID, "accountstatement_view"))
        )
        d.execute_script("arguments[0].scrollIntoView({block:'center'});", view_btn)
        time.sleep(0.3)
        try:
            view_btn.click()
        except ElementClickInterceptedException:
            d.execute_script("arguments[0].click();", view_btn)

        # 7) Export CSV (id: accountstatement_csvAcctStmt)
        csv_btn = WebDriverWait(d, 20).until(
            EC.element_to_be_clickable((By.ID, "accountstatement_csvAcctStmt"))
        )
        d.execute_script("arguments[0].scrollIntoView({block:'center'});", csv_btn)
        time.sleep(0.3)
        try:
            csv_btn.click()
        except ElementClickInterceptedException:
            d.execute_script("arguments[0].click();", csv_btn)

        # 8) Wait for the CSV download to appear in this worker's download dir
        csv_path = self.wait_newest_file(".csv", timeout=60.0)
        if not csv_path:
            raise TimeoutException("Timed out waiting for IOB CSV download")

        # 9) Upload to AutoBank in a new tab (robust with retries)
        original_handle = d.current_window_handle
        d.execute_script("window.open('about:blank');")
        autobank_handle = [h for h in d.window_handles if h != original_handle][-1]

        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            try:
                d.switch_to.window(autobank_handle)
                # Reuse shared client; legacy code always used "IOB" label even for corp uploads
                AutoBankClient(d).upload("IOB", acct_no, csv_path)
                self.info(f"✅ AutoBank upload succeeded (attempt {attempt}/{max_attempts})")
                break
            except Exception as e:
                self.error(
                    f"⚠️ AutoBank upload failed (attempt {attempt}/{max_attempts}): "
                    f"{type(e).__name__}: {e}"
                )
                try:
                    self.screenshot_all_tabs("AutoBank upload failed")
                except Exception:
                    pass
                if attempt == max_attempts:
                    # close upload tab & bubble up
                    try:
                        d.close()
                    except Exception:
                        pass
                    d.switch_to.window(original_handle)
                    raise
                time.sleep(2)

        # 10) Return to IOB tab and continue loop
        try:
            d.switch_to.window(original_handle)
            # ensure upload tab is closed
            for h in list(d.window_handles):
                if h != original_handle:
                    d.switch_to.window(h)
                    d.close()
            d.switch_to.window(original_handle)
        except Exception:
            pass

    def _balance_enquiry(self) -> None:
        d = self.driver

        # 1) Make sure we're on the IOB tab
        if self.iob_win:
            d.switch_to.window(self.iob_win)

        # 2) Scroll back to the very top (so the nav is visible)
        d.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.5)

        # 3) Locate the Balance Enquiry link (IOB can be slow, so 60s wait)
        balance_link = WebDriverWait(d, 60).until(
            EC.element_to_be_clickable((By.LINK_TEXT, "Balance Enquiry"))
        )

        # 4) Scroll it into view (center of viewport)
        d.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", balance_link
        )
        time.sleep(0.3)

        # 5) Click (with JS fallback)
        try:
            balance_link.click()
        except Exception:
            d.execute_script("arguments[0].click();", balance_link)

        # 6) Now click the specific account link to trigger the popup
        acctno = (self.cred.get("account_number") or "").strip()
        try:
            wait_long = WebDriverWait(d, 180)
            if acctno:
                # Match the anchor whose href contains 'getBalance' and whose text contains the account number
                acct_link = wait_long.until(
                    EC.element_to_be_clickable(
                        (
                            By.XPATH,
                            f"//a[contains(@href,'getBalance') and contains(.,'{acctno}')]",
                        )
                    )
                )
            else:
                # Fallback: first getBalance link if account_number not configured
                acct_link = wait_long.until(
                    EC.element_to_be_clickable(
                        (By.XPATH, "//a[contains(@href,'getBalance')]")
                    )
                )

            d.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", acct_link
            )
            time.sleep(0.2)
            try:
                acct_link.click()
            except Exception:
                d.execute_script("arguments[0].click();", acct_link)
        except TimeoutException:
            self.info("Balance enquiry: account link not found, skipping this cycle.")
            return

        # 7) Wait for the popup content inside #dialogtbl and read the available balance
        tbl = WebDriverWait(d, 180).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "#dialogtbl table tr.querytr td")
            )
        )
        available = (tbl.text or "").strip()
        if available:
            self.info(f"💰: {available}")
            self.last_balance = available

        # 8) Remove the modal overlay & dialog so nothing blocks future clicks
        try:
            d.execute_script(
                "document.querySelectorAll('.ui-widget-overlay, #dialogtbl').forEach(el => el.remove());"
            )
        except Exception:
            pass

        # 9) Click “Account statement” again (or just navigate there directly) for next cycle
        try:
            stmt_link = WebDriverWait(d, 60).until(
                EC.element_to_be_clickable((By.LINK_TEXT, "Account statement"))
            )
            d.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", stmt_link
            )
            time.sleep(0.2)
            try:
                stmt_link.click()
            except Exception:
                d.execute_script("arguments[0].click();", stmt_link)
        except Exception:
            # not critical; next cycle will navigate there anyway
            pass
