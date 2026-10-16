"""Key validation must work in frozen Python builds without OpenSSL PBKDF2."""
import hashlib
import hmac
import struct

import pytest

from backup.decryptor import _verify_enc_key
from engine.decrypt import derive_mac_key
from engine.services.config_cipher_extract import verify_enc_key as verify_cipher
from engine.services.wechat_key_extract import verify_enc_key as verify_wechat
from key_scan import verify_enc_key as verify_scan


KEY = bytes(range(32))
SALT = bytes(range(16))


def test_mac_key_matches_stdlib_without_hashlib_pbkdf2(monkeypatch):
    expected = hashlib.pbkdf2_hmac(
        'sha512', KEY, bytes(b ^ 0x3A for b in SALT), 2, dklen=32)
    monkeypatch.delattr(hashlib, 'pbkdf2_hmac')

    assert derive_mac_key(KEY, SALT) == expected


@pytest.mark.parametrize('verify', [
    verify_cipher, verify_wechat, verify_scan, _verify_enc_key,
], ids=['config-cipher', 'wechat-extract', 'key-scan', 'backup'])
def test_key_validation_without_hashlib_pbkdf2(verify, monkeypatch):
    mac_key = hashlib.pbkdf2_hmac(
        'sha512', KEY, bytes(b ^ 0x3A for b in SALT), 2, dklen=32)
    page = bytearray(4096)
    page[:16] = SALT
    page[16:4032] = b'\xAB' * 4016
    page[4032:] = hmac.new(
        mac_key, page[16:4032] + struct.pack('<I', 1), hashlib.sha512).digest()
    monkeypatch.delattr(hashlib, 'pbkdf2_hmac')

    assert verify(KEY, bytes(page))
    assert not verify(b'\xFF' * 32, bytes(page))
    page[20] ^= 1
    assert not verify(KEY, bytes(page))
