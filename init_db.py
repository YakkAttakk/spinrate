"""Run once at deploy time to create tables if they don't exist."""
from app import init_db
init_db()
print("Database initialised.")
