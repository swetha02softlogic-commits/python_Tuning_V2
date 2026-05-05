#!/usr/bin/env python3
"""
Run once at build time:
  python3 encrypt_script.py
Reads  : tuning_mainwindow.py
Writes : tuning_mainwindow.enc  (embedded into .qrc)
Prints : the 32-byte AES key as a C++ array  (paste into main.cpp)
"""

import os, struct
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY  = os.urandom(32)          # AES-256
NONCE = os.urandom(12)         # GCM standard nonce

with open("tuning_mainwindow.py", "rb") as f:
    plaintext = f.read()

aesgcm = AESGCM(KEY)
ciphertext = aesgcm.encrypt(NONCE, plaintext, None)

# File format:  [ 12-byte nonce ][ ciphertext+tag ]
with open("tuning_mainwindow.enc", "wb") as f:
    f.write(NONCE + ciphertext)

print("=== PASTE THIS INTO main.cpp ===\n")
print("static const unsigned char ENC_KEY[32] = {")
print("    " + ", ".join(f"0x{b:02x}" for b in KEY))
print("};\n")
print(f"Encrypted OK — {len(plaintext)} bytes → tuning_mainwindow.enc")
