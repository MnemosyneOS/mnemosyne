"""Crypto plugin for Mnemosyne OS — Fernet-based field encryption.

Uses the ``cryptography`` library (specifically ``Fernet``) to
symmetrically encrypt sensitive memory fields (default: ``content``).
The key is read from the ``MNEMOSYNE_CRYPTO_KEY`` environment variable
or from the brain's configuration; if absent the plugin degrades
gracefully and does **not** encrypt (so the zero-dependency core is
never broken).

The brain calls ``encrypt(field, value)`` before storing and
``decrypt(field, encrypted_value)`` after loading for any field the
plugin declares as ``sensitive_fields``.

Usage::

    brain = MemoryBrain(dir, plugins=["crypto"])
"""
import os
import json
import base64

# Graceful degradation: cryptography is optional
try:
    from cryptography.fernet import Fernet
    _HAS_CRYPTO = True
except ImportError:
    Fernet = None
    _HAS_CRYPTO = False

from storage.plugin_sdk import CryptoPlugin

__all__ = ["CryptoPluginFernet", "register", "get_plugin_class"]


class CryptoPluginFernet(CryptoPlugin):
    """Fernet-based symmetric encryption for memory fields."""

    name = "crypto"
    description = "Fernet symmetric encryption for sensitive memory fields"

    # Fields this plugin treats as sensitive (encrypted at rest)
    sensitive_fields = ("content",)

    def __init__(self, brain=None, key=None):
        super().__init__(brain)
        self._fernet = None
        self._key = key
        if _HAS_CRYPTO:
            resolved = self._resolve_key()
            if resolved:
                try:
                    self._fernet = Fernet(resolved)
                except Exception:
                    self._fernet = None

    def _resolve_key(self):
        if self._key:
            return self._key
        env_key = os.environ.get("MNEMOSYNE_CRYPTO_KEY")
        if env_key:
            return env_key.encode("utf-8") if isinstance(env_key, str) else env_key
        # Try brain config
        if self.brain is not None:
            cfg = getattr(self.brain, "config", None)
            if cfg and isinstance(cfg, dict) and cfg.get("crypto_key"):
                ck = cfg["crypto_key"]
                if isinstance(ck, str):
                    return ck.encode("utf-8")
                return ck
        return None

    @property
    def available(self):
        return self._fernet is not None

    def get_key(self):
        return self._key

    def encrypt(self, field, value, **kwargs):
        if not self._fernet:
            return value  # no encryption available — return as-is
        if field not in self.sensitive_fields:
            return value
        if value is None:
            return None
        if not isinstance(value, (str, bytes)):
            value = json.dumps(value, ensure_ascii=False)
        data = value.encode("utf-8") if isinstance(value, str) else value
        return self._fernet.encrypt(data).decode("utf-8")

    def decrypt(self, field, encrypted_value, **kwargs):
        if not self._fernet:
            return encrypted_value
        if field not in self.sensitive_fields:
            return encrypted_value
        if encrypted_value is None:
            return None
        try:
            data = self._fernet.decrypt(encrypted_value.encode("utf-8"))
            return data.decode("utf-8")
        except Exception:
            return encrypted_value  # return as-is if decryption fails


def register(brain):
    """Plugin entry point — returns a CryptoPluginFernet instance."""
    return CryptoPluginFernet(brain)


def get_plugin_class():
    return CryptoPluginFernet
