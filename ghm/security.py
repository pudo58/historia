import base64
import hashlib
import os
from typing import Protocol

from cryptography.fernet import Fernet


class KeyBackend(Protocol):
    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...


class SecretStore:
    """Encrypts host credentials; plaintext is never persisted or returned."""

    def __init__(self, master_key: str) -> None:
        digest = hashlib.sha256(master_key.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    @classmethod
    def from_environment(cls, key_backend: KeyBackend | None = None) -> "SecretStore":
        configured = os.environ.get("GHM_MASTER_KEY")
        if configured:
            return cls(configured)
        if key_backend is None:
            try:
                import keyring

                key_backend = keyring
            except ImportError as exc:
                raise RuntimeError("Set GHM_MASTER_KEY or install a keyring backend.") from exc
        key = key_backend.get_password("gpu-host-manager", "master-key")
        if key is None:
            key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
            key_backend.set_password("gpu-host-manager", "master-key", key)
        return cls(key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
