import hashlib
import secrets
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
try:
    from .config import get_settings
except ImportError:
    from config import get_settings


class LocalVault:
    """Development vault. Replace this with KMS envelope encryption in production."""
    def __init__(self) -> None:
        self.fernet = Fernet(get_settings().vault_master_key.encode())

    def encrypt(self, value: bytes) -> str:
        return self.fernet.encrypt(value).decode()


def generate_ssh_keypair() -> tuple[str, bytes]:
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption())
    public_key = private_key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    return public_key, private_bytes


def new_bootstrap_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, token_digest(token)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
