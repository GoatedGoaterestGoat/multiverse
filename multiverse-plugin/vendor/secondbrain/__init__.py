"""Vendored SecondBrain wire-protocol + auth contract.

These two modules — :mod:`secondbrain.protocol` and :mod:`secondbrain.auth` —
are **byte-identical copies** of the authoritative sources in the SecondBrain
repo:

    SecondBrain/src/sb_host/protocol.py  ->  vendor/secondbrain/protocol.py
    SecondBrain/src/sb_host/auth.py      ->  vendor/secondbrain/auth.py

They are the *contract* of the seam (``SecondBrain/docs/INFRA.md`` §6/§10): the
Multiverse-side connector must produce and consume exactly these frame shapes,
and every signed/MAC'd transcript + the X25519->HKDF->ChaCha20-Poly1305 channel
must be the same bytes on both ends. So we vendor rather than re-implement, and
keep the copies **verbatim** — do not edit them here. To update, re-copy from
the SecondBrain repo and diff to confirm the wire didn't change:

    cp SecondBrain/src/sb_host/{protocol,auth}.py \\
       Multiverse/multiverse-plugin/vendor/secondbrain/

Only ``cryptography`` (for auth) and the stdlib are required; neither module
imports anything from ``sb_host``, so they stand alone here.
"""
