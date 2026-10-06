"""
Very lightweight manager "login" — this is an internal single-agency tool,
not a public product, so the goal is just to keep managers' folders apart,
not to build real security. First login with a given name registers that
name with the given code; later logins must match the same code.
"""
import hashlib
import hmac
import os


def hash_code(code: str) -> str:
    salt = os.environ.get("CODE_SALT", "qa-tool-static-salt")
    return hashlib.pbkdf2_hmac("sha256", code.encode(), salt.encode(), 100_000).hex()


def verify_code(code: str, code_hash: str) -> bool:
    return hmac.compare_digest(hash_code(code), code_hash)
