"""
Configuration management with Fernet encryption for sensitive fields.
"""

import hashlib
import json
import logging
import os
import stat
from typing import Optional

from cryptography.fernet import Fernet

from .constants import CONFIG_DIR, CONFIG_FILE

logger = logging.getLogger(__name__)

ENCRYPTION_KEY_ENV = "AG_ENCRYPTION_KEY"


def _get_or_create_key() -> bytes:
    """Resolve encryption key: env var first, then per-machine key file."""
    env_key = os.environ.get(ENCRYPTION_KEY_ENV)
    if env_key:
        return hashlib.sha256(env_key.encode()).digest()[:32]

    key_path = os.path.join(CONFIG_DIR, ".bridge_key")
    if os.path.exists(key_path):
        return open(key_path, "rb").read()

    key = Fernet.generate_key()
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(key_path, "wb") as f:
        f.write(key)
    try:
        os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return key


def _fernet() -> Fernet:
    raw = _get_or_create_key()
    if len(raw) == 44:
        return Fernet(raw)
    return Fernet(Fernet.generate_key())


def encrypt_value(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_value(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except Exception:
        return value


def load_config() -> dict:
    """Load config from disk, decrypting sensitive fields."""
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, "r") as f:
            raw = json.load(f)
        if 'avigilon' in raw and raw['avigilon'].get('password'):
            raw['avigilon']['password'] = decrypt_value(raw['avigilon']['password'])
        if 'accessgrid' in raw and raw['accessgrid'].get('api_secret'):
            raw['accessgrid']['api_secret'] = decrypt_value(raw['accessgrid']['api_secret'])
        return raw
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return {}


def save_config(config: dict):
    """Save config to disk, encrypting sensitive fields."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    to_save = json.loads(json.dumps(config))
    if 'avigilon' in to_save and to_save['avigilon'].get('password'):
        to_save['avigilon']['password'] = encrypt_value(to_save['avigilon']['password'])
    if 'accessgrid' in to_save and to_save['accessgrid'].get('api_secret'):
        to_save['accessgrid']['api_secret'] = encrypt_value(to_save['accessgrid']['api_secret'])
    with open(CONFIG_FILE, "w") as f:
        json.dump(to_save, f, indent=2)
    logger.info(f"Config saved to {CONFIG_FILE}")
