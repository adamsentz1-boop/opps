"""Run one scan + processing cycle from the command line (no web server needed)."""
from __future__ import annotations

import json

from app.db import init_db, session_scope
from app.pipeline import run_scan

if __name__ == "__main__":
    init_db()
    with session_scope() as db:
        print(json.dumps(run_scan(db), indent=2, default=str))
