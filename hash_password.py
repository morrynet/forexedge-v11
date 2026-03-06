#!/usr/bin/env python3
"""
Utility: Generate a hashed admin password for ADMIN_PASS_HASH env var.

Usage:
    python hash_password.py
    python hash_password.py mypassword
"""
import sys
from werkzeug.security import generate_password_hash

def main():
    if len(sys.argv) > 1:
        password = sys.argv[1]
    else:
        import getpass
        password = getpass.getpass("Enter admin password: ")
        confirm  = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords do not match."); sys.exit(1)

    hashed = generate_password_hash(password)
    print("\nAdd this to your environment (Render dashboard / .env):\n")
    print(f"ADMIN_PASS_HASH={hashed}\n")

if __name__ == "__main__":
    main()
