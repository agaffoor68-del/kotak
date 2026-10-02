"""Diagnose admin access: which users exist, and does a password match?"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.core.security import verify_password

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "var", "master.sqlite3")
connection = sqlite3.connect(DB)
connection.row_factory = sqlite3.Row

print("=== users ===")
for row in connection.execute("SELECT id, email, role, is_active, password_hash FROM users"):
    print(f"email={row['email']!r} role={row['role']!r} active={row['is_active']}")
    for candidate in ("AlphaDemo@2026", "admin", "admin123", "password"):
        if verify_password(candidate, row["password_hash"]):
            print(f"   MATCHING PASSWORD -> {candidate!r}")
    else:
        print("   (no candidate password matched)")

print("=== broker_accounts ===")
try:
    rows = list(connection.execute("SELECT account_id, label FROM broker_accounts"))
    print(f"count={len(rows)}")
    for row in rows:
        print(f"   account_id={row['account_id']!r} label={row['label']!r}")
except sqlite3.OperationalError as error:
    print(f"table missing: {error}")

print("=== api_keys ===")
try:
    rows = list(connection.execute("SELECT id, name FROM api_keys"))
    print(f"count={len(rows)}")
except sqlite3.OperationalError as error:
    print(f"table missing: {error}")
connection.close()
print("done")