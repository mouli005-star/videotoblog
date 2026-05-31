import os

from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()


def _f():
    return Fernet(os.getenv("ENCRYPTION_KEY").encode())


def encrypt(value: str) -> str:
    return _f().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return _f().decrypt(value.encode()).decode()
