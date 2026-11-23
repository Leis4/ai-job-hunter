"""Project paths. Everything runtime lives under data/ and stays local."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Tests and portable installs may relocate config/data without touching code.
CONFIG = Path(os.environ.get("JOB_HUNTER_CONFIG_DIR") or (ROOT / "config")).resolve()
DATA = Path(os.environ.get("JOB_HUNTER_DATA_DIR") or (ROOT / "data")).resolve()
INBOX = Path(os.environ.get("JOB_HUNTER_INBOX_DIR") or (ROOT / "inbox")).resolve()
PROCESSED = INBOX.parent / "processed"
EXPORTS = DATA.parent / "exports"
OUTPUT = DATA.parent / "output"
LOGS = DATA.parent / "logs"
RESUME_FILES = OUTPUT / "resumes"
BROWSER_PROFILE = DATA / "browser_profile"
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
HH_BROWSER_PROFILE = LOCALAPPDATA / "AI-Job-Hunter" / "hh-browser-profile"
HH_HISTORY_CAPTURE = DATA / "hh_history_capture"
DB_PATH = DATA / "jobs.sqlite3"
TOKEN_PATH = DATA / "web_token.txt"


def ensure_dirs() -> None:
    for path in (CONFIG, DATA, INBOX, PROCESSED, EXPORTS, OUTPUT, LOGS, RESUME_FILES):
        path.mkdir(parents=True, exist_ok=True)
