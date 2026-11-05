import os

import psycopg
from dotenv import load_dotenv

load_dotenv()

def get_conn() -> psycopg.Connection:
    """Open a connection to the database using  DATABASE_URL environment variable."""
    return psycopg.connect(os.getenv("DATABASE_URL"), connect_timeout=5)

if __name__ == "__main__":
    with get_conn() as conn:
        version = conn.execute("SELECT version()").fetchone()[0]
        tables = conn.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'").fetchone()[0]
        print(f"Connected to database: {version.split(' ')[0]}")
        print(f"Number of tables in public schema: {tables}")