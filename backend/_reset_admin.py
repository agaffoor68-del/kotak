"""Create or repair an administrator account, so you can always get back in.

Use it when the first admin's password has been lost, when the bootstrap env vars
were never set, or when a deployment needs the original account re-enabled.

    python backend/_reset_admin.py admin@alphatrade.local 'Your-New-Password'
    python backend/_reset_admin.py viewer@alphatrade.local 'Read-Only-9' --role viewer

The password is written as a PBKDF2 hash, exactly as the login route expects; the
plaintext is never stored. Pass `--email`/`--password` as flags to keep the value
out of your shell history, or set ADMIN_EMAIL/ADMIN_PASSWORD in the environment.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Make `python backend/_reset_admin.py` work from any directory, without
# requiring PYTHONPATH=. the way the smoke tests do.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
log = logging.getLogger("alphatrade.reset-admin")

# default_password is only a fallback; a blank value is rejected below.
ROLES = ("admin", "trader", "viewer")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or update an AlphaTradePro user account.")
    parser.add_argument("email", nargs="?", default=os.getenv("ADMIN_EMAIL", ""),
                        help="Email of the account to create or update.")
    parser.add_argument("password", nargs="?", default=os.getenv("ADMIN_PASSWORD", ""),
                        help="New password (minimum 10 characters).")
    parser.add_argument("--email", dest="email_flag", default=None, help="Email (overrides the positional).")
    parser.add_argument("--password", dest="password_flag", default=None, help="Password (overrides the positional).")
    parser.add_argument("--role", default="admin", choices=ROLES, help="Role to enforce (default: admin).")
    args = parser.parse_args()

    email = (args.email_flag or args.email or "").strip().lower()
    password = args.password_flag or args.password or ""

    if not email:
        parser.error("an email is required (positional, --email, or ADMIN_EMAIL)")
    if len(password) < 10:
        parser.error("the password must be at least 10 characters")

    # Imported here so `--help` never needs the application environment.
    import backend.app_bootstrap  # noqa: F401  (loads .env before settings is read)

    from backend.core.config import settings
    from backend.core.database import app_cursor, now, row_to_dict
    from backend.core.security import hash_password

    log.info("Using state database %s", settings.state_file)

    password_hash = hash_password(password)
    with app_cursor() as cursor:
        cursor.execute("SELECT * FROM users WHERE email = ?", (email,))
        existing = row_to_dict(cursor.fetchone())

        if existing is None:
            user_id = uuid.uuid4().hex
            cursor.execute(
                "INSERT INTO users (id, email, password_hash, role, is_active, created_at) VALUES (?,?,?,?,1,?)",
                (user_id, email, password_hash, args.role, now()),
            )
            log.info("Created %s (%s) as %s", email, user_id, args.role)
            action = "created"
        else:
            cursor.execute(
                "UPDATE users SET password_hash = ?, role = ?, is_active = 1 WHERE id = ?",
                (password_hash, args.role, existing["id"]),
            )
            log.info("Updated %s (%s) to role %s and a new password", email, existing["id"], args.role)
            action = "updated"

    print(f"\n{action}: {email}  role={args.role}  active=yes")
    print("Sign in at http://localhost:3000/login with the password you just set.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())