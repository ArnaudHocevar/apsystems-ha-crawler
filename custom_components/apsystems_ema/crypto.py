"""Crypto helpers for the APsystems EMA login handshake.

The EMA login page encrypts the username/password client-side with an
AES-128-CBC key/IV that are themselves RSA (PKCS1v1.5) encrypted with a
fixed public key embedded in the page's JavaScript. This module re-implements
that scheme server-side using the ``cryptography`` package so Home Assistant
does not need a browser/JS engine to log in.

Scheme (reverse engineered, see PR description for full details):
    * AES key: a random 8-byte value, hex-encoded to a 16-character string.
      The UTF-8 bytes of that 16-char hex STRING (not the raw 8 bytes) are
      the actual AES-128 key.
    * AES IV: a random decimal integer in [0, 10**16), stringified and
      zero-padded on the left to exactly 16 characters. The UTF-8 bytes of
      that string are the IV.
    * username/password are AES-128-CBC encrypted (zero padding: pad with
      0x00 bytes up to a multiple of 16, no extra block if already aligned)
      then hex-encoded (lowercase).
    * The AES key/IV strings are themselves RSA PKCS1v1.5 encrypted with the
      site's public key and base64-encoded to build the `key`/`version` form
      fields.

Double URL-encoding quirk: the real browser's hidden form fields hold
``encodeURIComponent(ciphertext)`` as their DOM value, and the browser's own
form submission then URL-encodes those values AGAIN when building the
``application/x-www-form-urlencoded`` body. Net effect:
    * username/password (hex strings, no reserved chars) end up effectively
      single-encoded on the wire.
    * key/version (base64, containing ``+ / =``) must be URL-encoded TWICE
      on the wire.
This module's ``build_login_payload`` returns the final, already-correctly
percent-encoded form body as a single string, matching the real browser's
wire format exactly.
"""
from __future__ import annotations

import secrets
from urllib.parse import quote

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# The site's RSA-2048 public key (X.509 SubjectPublicKeyInfo DER, base64).
EMA_PUBLIC_KEY_B64 = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAgdwBhVodMQ84lYZhDSGO"
    "UDQAks+NMa7WQ83mR1OyHiIWtZ1wWAh4H7fclkdNS3lWCmDH9ldF7Kf6JlEvZTc0"
    "Textv+YMLXO2gdDIoBvg7vlhY4HxOjXUIFQ+s7cWRrmEIgVVnTBLZU1GMC8zld7W"
    "H9v9EYCAqK7rvGJP0STZ/g6BP8RGJKhdpY6b+ndMXRUBYwkqy8m1SDJHm1FeHSLQ"
    "WTaWbP5pz1yrGkkwvx+pib6wli+WE70/uPHp0zXZK5iUwmRQfOkTjDOGJyEE1dqk"
    "fHDTqne5ED81M4fCIEFYhyvnr1rifVJKHCDRGYQpJ0CiffjjH1ZOGSIN4JPG1EEI"
    "jQIDAQAB"
)


def _load_public_key():
    import base64

    der = base64.b64decode(EMA_PUBLIC_KEY_B64)
    return serialization.load_der_public_key(der)


def generate_aes_key() -> str:
    """Return a random 16-character hex string used as the AES-128 key string."""
    return secrets.token_hex(8)


def generate_aes_iv() -> str:
    """Return a random decimal string in [0, 10**16), zero-padded to 16 chars."""
    value = secrets.randbelow(10**16)
    return str(value).zfill(16)


def zero_pad(data: bytes, block_size: int = 16) -> bytes:
    """Pad ``data`` with 0x00 bytes to a multiple of ``block_size``.

    No extra block is added if the data is already aligned.
    """
    remainder = len(data) % block_size
    if remainder == 0:
        return data
    return data + b"\x00" * (block_size - remainder)


def aes_encrypt_hex(plaintext: str, key_str: str, iv_str: str) -> str:
    """AES-128-CBC encrypt ``plaintext`` with zero padding, return lowercase hex."""
    key_bytes = key_str.encode("utf-8")
    iv_bytes = iv_str.encode("utf-8")
    data = zero_pad(plaintext.encode("utf-8"))
    cipher = Cipher(algorithms.AES(key_bytes), modes.CBC(iv_bytes))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(data) + encryptor.finalize()
    return ciphertext.hex()


def rsa_encrypt_b64(plaintext: str, public_key=None) -> str:
    """RSA PKCS1v1.5 encrypt ``plaintext`` with the EMA public key, base64-encoded."""
    import base64

    if public_key is None:
        public_key = _load_public_key()
    ciphertext = public_key.encrypt(plaintext.encode("utf-8"), asym_padding.PKCS1v15())
    return base64.b64encode(ciphertext).decode("ascii")


def double_quote(value: str) -> str:
    """URL-encode ``value`` twice (matches the browser's double-encoding quirk)."""
    return quote(quote(value, safe=""), safe="")


def build_login_payload(username: str, password: str, today: str) -> tuple[str, str]:
    """Build the final, pre-encoded login form body and the raw verify code value.

    Returns a tuple of ``(form_body, content_type)`` where ``form_body`` is the
    exact percent-encoded ``application/x-www-form-urlencoded`` body to send to
    ``loginEMA.action``, already including the double-URL-encoding quirk for
    the ``key``/``version`` fields.
    """
    aes_key = generate_aes_key()
    aes_iv = generate_aes_iv()

    username_hex = aes_encrypt_hex(username, aes_key, aes_iv)
    password_hex = aes_encrypt_hex(password, aes_key, aes_iv)

    key_b64 = rsa_encrypt_b64(aes_key)
    version_b64 = rsa_encrypt_b64(aes_iv)

    fields = [
        ("today", quote(today, safe="")),
        ("code", ""),
        ("humanVerifyFlag", ""),
        ("userId", ""),
        ("username", quote(username_hex, safe="")),
        ("key", double_quote(key_b64)),
        ("version", double_quote(version_b64)),
        ("password", quote(password_hex, safe="")),
        ("verifyCode", quote(" ", safe="")),
    ]
    body = "&".join(f"{name}={value}" for name, value in fields)
    return body, "application/x-www-form-urlencoded"
