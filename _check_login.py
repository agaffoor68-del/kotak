import os, secrets, uuid
os.environ["SECRET_KEY"] = "test-secret"
os.environ["CREDENTIAL_ENCRYPTION_KEY"] = "test-encryption-key"
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["DATA_DIR"] = "./var"
from fastapi.testclient import TestClient
from backend.main import app
from backend.core.database import app_cursor, now
from backend.core.security import hash_password
c = TestClient(app)  # no `with` => lifespan (startup) does not run
email = f"check-{secrets.token_hex(3)}@example.com"
with app_cursor() as cur:
    cur.execute("INSERT INTO users (id,email,password_hash,role,is_active,created_at) VALUES (?,?,?,?,1,?)", (uuid.uuid4().hex, email, hash_password("testpass1234"), "viewer", now()))
for label, body in [("valid", {"email": email, "password": "testpass1234"}), ("wrongpw", {"email": email, "password": "nope"}), ("gmail", {"email": "someone@gmail.com", "password": "x"})]:
    r = c.post("/api/v1/auth/login", json=body)
    print(label, r.status_code, r.text[:160])
with app_cursor() as cur:
    cur.execute("DELETE FROM users WHERE email=?", (email,))
print("done")
