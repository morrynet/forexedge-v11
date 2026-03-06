"""Run this once on first deploy to initialise DB tables."""
from app import init_db
if __name__ == "__main__":
    init_db()
    print("Database initialised.")
