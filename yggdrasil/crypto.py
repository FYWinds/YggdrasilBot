from functools import cache

from cryptography.fernet import Fernet

from yggdrasil.config import APP_CONFIG


@cache
def _fernet() -> Fernet:
    return Fernet(APP_CONFIG.secret_key.encode())


def encrypt(value: str) -> bytes:
    return _fernet().encrypt(value.encode())


def decrypt(token: bytes) -> str:
    return _fernet().decrypt(token).decode()


def mask(value: str) -> str:
    return f"…{value[-4:]}" if len(value) > 4 else "…"
