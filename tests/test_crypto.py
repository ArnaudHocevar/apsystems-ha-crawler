"""Unit tests for the RSA/AES login crypto and the double-URL-encoding quirk."""
from __future__ import annotations

from urllib.parse import unquote

from Crypto.Cipher import PKCS1_v1_5 as PKCS1_v1_5_cipher
from Crypto.PublicKey import RSA
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from custom_components.apsystems_ema import crypto


def test_zero_pad_already_aligned():
    data = b"0123456789abcdef"  # exactly 16 bytes
    assert crypto.zero_pad(data) == data


def test_zero_pad_adds_zero_bytes_no_extra_block():
    data = b"hello"  # 5 bytes -> pad to 16, not 32
    padded = crypto.zero_pad(data)
    assert len(padded) == 16
    assert padded == b"hello" + b"\x00" * 11


def test_aes_encrypt_hex_matches_pycryptodome_cross_check():
    """Fixed key/iv vector: our AES-CBC/zero-pad implementation must match
    an independent AES implementation (pycryptodome) byte for byte."""
    from Crypto.Cipher import AES as PyAES

    key_str = "0123456789abcdef"
    iv_str = "0000000000000001"
    plaintext = "Hocevar arnaud"

    ours = crypto.aes_encrypt_hex(plaintext, key_str, iv_str)

    data = plaintext.encode("utf-8")
    remainder = len(data) % 16
    if remainder:
        data = data + b"\x00" * (16 - remainder)
    cipher = PyAES.new(key_str.encode("utf-8"), PyAES.MODE_CBC, iv_str.encode("utf-8"))
    theirs = cipher.encrypt(data).hex()

    assert ours == theirs


def test_aes_encrypt_hex_is_deterministic_for_fixed_key_iv():
    key_str = "fedcba9876543210"
    iv_str = "1234567890123456"
    out1 = crypto.aes_encrypt_hex("f$SMS6QL*oYqrP", key_str, iv_str)
    out2 = crypto.aes_encrypt_hex("f$SMS6QL*oYqrP", key_str, iv_str)
    assert out1 == out2
    assert all(c in "0123456789abcdef" for c in out1)


def test_generate_aes_key_and_iv_shapes():
    key = crypto.generate_aes_key()
    iv = crypto.generate_aes_iv()
    assert len(key) == 16
    int(key, 16)  # must be valid hex
    assert len(iv) == 16
    assert iv.isdigit()
    assert 0 <= int(iv) < 10**16


def test_rsa_encrypt_interop_with_pycryptodome_decrypt():
    """Encrypt with our `cryptography`-based PKCS1v1.5 implementation using a
    freshly generated test keypair, then decrypt with an independent library
    (pycryptodome) to confirm wire-format interoperability."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    plaintext = "abcdef0123456789"
    ciphertext_b64 = crypto.rsa_encrypt_b64(plaintext, public_key=public_key)

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    py_key = RSA.import_key(private_pem)
    cipher = PKCS1_v1_5_cipher.new(py_key)

    import base64

    decrypted = cipher.decrypt(base64.b64decode(ciphertext_b64), sentinel=None)
    assert decrypted.decode("utf-8") == plaintext


def test_double_quote_encodes_twice():
    value = "a+b/c=d"
    once = crypto.double_quote(value)
    # Decoding twice should get back the original; decoding once should NOT.
    assert unquote(unquote(once)) == value
    assert unquote(once) != value


def test_build_login_payload_double_encodes_key_and_version_single_encodes_rest():
    body, content_type = crypto.build_login_payload(
        "Hocevar arnaud", "f$SMS6QL*oYqrP", "2026-01-01 00:00:00"
    )
    assert content_type == "application/x-www-form-urlencoded"

    fields = dict(part.split("=", 1) for part in body.split("&"))

    assert set(fields) == {
        "today",
        "code",
        "humanVerifyFlag",
        "userId",
        "username",
        "key",
        "version",
        "password",
        "verifyCode",
    }

    # today/username/password/verifyCode: single-encoded on the wire.
    assert unquote(fields["today"]) == "2026-01-01 00:00:00"
    assert unquote(fields["verifyCode"]) == " "
    # hex strings contain no reserved chars, so single vs double encoding are
    # indistinguishable for them directly, but they must be valid lowercase hex
    # after a single decode.
    username_hex = unquote(fields["username"])
    password_hex = unquote(fields["password"])
    bytes.fromhex(username_hex)
    bytes.fromhex(password_hex)

    # key/version: base64 (contains +, /, or = in practice for a 2048-bit RSA
    # ciphertext) and MUST be double-encoded: decoding once should still
    # contain a leftover percent-escape, decoding twice yields valid base64.
    import base64

    key_once = unquote(fields["key"])
    key_twice = unquote(key_once)
    assert key_once != key_twice  # proves it really was double-encoded
    base64.b64decode(key_twice)  # must be valid base64 after exactly 2 decodes

    version_once = unquote(fields["version"])
    version_twice = unquote(version_once)
    assert version_once != version_twice
    base64.b64decode(version_twice)


def test_build_login_payload_field_order():
    body, _ = crypto.build_login_payload("user", "pass", "2026-01-01 00:00:00")
    names = [part.split("=", 1)[0] for part in body.split("&")]
    assert names == [
        "today",
        "code",
        "humanVerifyFlag",
        "userId",
        "username",
        "key",
        "version",
        "password",
        "verifyCode",
    ]
