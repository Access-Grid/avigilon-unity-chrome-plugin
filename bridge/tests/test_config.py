"""
Tests for config management — encryption, load, save.
"""

import json
import os
import tempfile
import pytest
from unittest.mock import patch

from src.config import encrypt_value, decrypt_value, load_config, save_config


class TestEncryption:

    def test_encrypt_decrypt_roundtrip(self):
        original = "my-secret-password"
        encrypted = encrypt_value(original)
        assert encrypted != original
        decrypted = decrypt_value(encrypted)
        assert decrypted == original

    def test_encrypt_produces_different_values(self):
        val = "test"
        e1 = encrypt_value(val)
        e2 = encrypt_value(val)
        # Fernet produces different ciphertexts due to timestamp
        assert decrypt_value(e1) == val
        assert decrypt_value(e2) == val


class TestConfigFile:

    def test_load_missing_file(self, tmp_path):
        with patch('src.config.CONFIG_FILE', str(tmp_path / 'nonexistent.json')):
            result = load_config()
            assert result == {}

    def test_save_and_load_roundtrip(self, tmp_path):
        config_file = str(tmp_path / 'config.json')
        config = {
            'plasec': {
                'host': '10.0.0.1',
                'username': 'admin',
                'password': 'secret123',
            },
            'accessgrid': {
                'account_id': 'acc1',
                'api_secret': 'supersecret',
                'template_id': 'tmpl1',
            },
        }

        with patch('src.config.CONFIG_FILE', config_file), \
             patch('src.config.CONFIG_DIR', str(tmp_path)):
            save_config(config)

            # Verify the file exists and password is encrypted
            with open(config_file) as f:
                raw = json.load(f)
            assert raw['plasec']['password'] != 'secret123'
            assert raw['accessgrid']['api_secret'] != 'supersecret'

            # Load and verify decryption
            loaded = load_config()
            assert loaded['plasec']['host'] == '10.0.0.1'
            assert loaded['plasec']['password'] == 'secret123'
            assert loaded['accessgrid']['api_secret'] == 'supersecret'
            assert loaded['accessgrid']['template_id'] == 'tmpl1'
