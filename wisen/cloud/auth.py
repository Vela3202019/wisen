"""
Secrets the server hands out or checks: login passwords, browser sessions,
pairing codes and device tokens. Only hashes are stored: scrypt for
passwords (people pick them), sha256 for the random tokens and codes (256 and
~50 bits of randomness, so a fast hash is enough).
"""
import base64
import hashlib
import hmac
import secrets

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}
# Unambiguous when read off a screen and typed: no 0/O, 1/I/L.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 10


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt${n}${r}${p}${salt}${digest}".format(
        **_SCRYPT, salt=base64.b64encode(salt).decode(), digest=base64.b64encode(digest).decode()
    )


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p))
    return hmac.compare_digest(actual, base64.b64decode(digest))


def new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_code() -> str:
    """A one-time pairing code, shown as XXXXX-XXXXX."""
    code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return f"{code[:5]}-{code[5:]}"


def normalize_code(code: str) -> str:
    return "".join(c for c in code.upper() if c.isalnum())
