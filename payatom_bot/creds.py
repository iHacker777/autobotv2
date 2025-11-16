from __future__ import annotations
import csv
from typing import Dict, Optional

# Map alias suffixes to the exact AutoBank dropdown labels.
# Adjust strings to match my portal's options precisely.
BANK_LABEL_BY_SUFFIX: Dict[str, str] = {
    "_tmb":     "TMB",
    "_iobcorp": "IOB Corporate",
    "_iob":     "IOB",
    "_kgb":     "KGB",
    "_idbi":    "IDBI",
    "_idfc":    "IDFC",
    "_canara":  "CANARA",
    "_cnrb":    "CANARA",
}

def infer_bank_label_from_alias(alias: str) -> str:
    a = alias.lower().strip()
    for suffix, label in BANK_LABEL_BY_SUFFIX.items():
        if a.endswith(suffix):
            return label
    # Fallback: take last token after '_' and uppercase (e.g., foo_xyz -> XYZ)
    if "_" in a:
        return a.rsplit("_", 1)[-1].upper()
    return a.upper()

def canonical_auth_id(username: str, login_id: str, user_id: str) -> Optional[str]:
    for v in (username or "", login_id or "", user_id or ""):
        v = v.strip()
        if v:
            return v
    return None

def load_creds(csv_path: str) -> Dict[str, dict]:
    """
    Loads a CSV with schema:
      alias,login_id,user_id,username,password,account_number
    Returns {alias: {auth_id, password, account_number, bank_label, raw:{...}}}
    """
    out: Dict[str, dict] = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        # Validate headers (order can vary; names must exist)
        required = {"alias", "login_id", "user_id", "username", "password", "account_number"}
        missing = required - set(h.lower() for h in reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV is missing columns: {', '.join(sorted(missing))}")

        for row in reader:
            alias = (row.get("alias") or "").strip()
            if not alias:
                continue  # skip blank

            login_id = row.get("login_id") or ""
            user_id = row.get("user_id") or ""
            username = row.get("username") or ""
            password = (row.get("password") or "").strip()
            account_number = (row.get("account_number") or "").strip()

            auth_id = canonical_auth_id(username, login_id, user_id)
            if not auth_id or not password or not account_number:
                # Skip incomplete rows but keep going
                # (Optionally, log this somewhere)
                continue

            bank_label = infer_bank_label_from_alias(alias)

            out[alias] = {
                "alias": alias,
                "auth_id": auth_id,
                "password": password,
                "account_number": account_number,
                "bank_label": bank_label,
                # Keep raw fields in case a specific bank needs them:
                "login_id": login_id.strip(),
                "user_id": user_id.strip(),
                "username": username.strip(),
            }
    return out
