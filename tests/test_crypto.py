"""Tests for core encryption service."""
import pytest
from app.services.crypto import encrypt_data, decrypt_data


class TestEncryptDecrypt:
    def test_roundtrip(self):
        original = "sk-test-key-12345"
        encrypted = encrypt_data(original)
        decrypted = decrypt_data(encrypted)
        assert decrypted == original

    def test_different_values(self):
        values = ["password123", "ghp_abc123", "sk-proj-xyz", "", "a" * 1000]
        for v in values:
            assert decrypt_data(encrypt_data(v)) == v

    def test_none_input(self):
        assert encrypt_data(None) is None
        assert decrypt_data(None) is None

    def test_encryption_deterministic_with_same_key(self):
        """Fernet produces different ciphertext each time (new IV)."""
        v = "secret"
        e1 = encrypt_data(v)
        e2 = encrypt_data(v)
        # Ciphertext should differ (Fernet uses random IV)
        assert e1 != e2
        # But decryption yields same plaintext
        assert decrypt_data(e1) == decrypt_data(e2) == v

    def test_ciphertext_is_not_plaintext(self):
        v = "sensitive-data"
        e = encrypt_data(v)
        assert e != v
        assert v not in e
