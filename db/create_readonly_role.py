"""Create the read-only login the chatbot's run_sql tool uses, and save its URL in .env.

The login can only read the vendor and geography tables (not the app_* login tables),
every transaction is read-only, and a query is stopped after 15 seconds. Re-running it
sets a new password.

    python db/create_readonly_role.py
"""
import os
import re
import secrets
import psycopg
from psycopg import sql
from dotenv import load_dotenv

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
load_dotenv(ENV_FILE)
VENDOR_DB_URL = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost:5432/primvmon_zoho_vendors")
ROLE = "vendor_reader"
ENV_KEY = "READONLY_DATABASE_URL"


def save_env(url):
    with open(ENV_FILE) as f:
        text = f.read()
    line = f"{ENV_KEY}={url}"
    if re.search(rf"^{ENV_KEY}=", text, re.M):
        text = re.sub(rf"^{ENV_KEY}=.*$", line, text, flags=re.M)
    else:
        text = text + ("" if text.endswith("\n") or not text else "\n") + line + "\n"
    with open(ENV_FILE, "w") as f:
        f.write(text)


def main():
    password = secrets.token_urlsafe(24)
    with psycopg.connect(VENDOR_DB_URL) as conn:
        database = conn.info.dbname
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,)).fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(sql.SQL(verb + " ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(ROLE), sql.Literal(password)))
        conn.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(sql.Identifier(ROLE)))
        conn.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = '15s'").format(sql.Identifier(ROLE)))
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), sql.Identifier(ROLE)))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(ROLE)))
        tables = [name for (name,) in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename NOT LIKE 'app\\_%'")]
        for table in tables:
            conn.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(sql.Identifier(table), sql.Identifier(ROLE)))
        # Tables the import scripts re-create later stay readable
        conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {}").format(sql.Identifier(ROLE)))
        host, port = conn.info.host, conn.info.port
    save_env(f"postgresql://{ROLE}:{password}@{host}:{port}/{database}")
    print(f"{ROLE}: can read {len(tables)} tables; {ENV_KEY} saved in .env")


if __name__ == "__main__":
    main()
