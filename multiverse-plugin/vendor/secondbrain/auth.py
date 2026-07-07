"""Authentication crypto (decision **D2**, ``docs/INFRA.md`` §6) — implemented.

Airtight, edge-blind mutual auth over a TLS-terminating tunnel:

* **Static identities** — Ed25519. SB has one server keypair (fingerprint pinned into every
  connector out-of-band); each device has its own keypair (private key never leaves it).
* **Per-connection channel** — X25519 ephemerals → ECDH → HKDF-SHA256 → a ChaCha20-Poly1305
  AEAD channel. Every frame after ``challenge`` is sealed, so the tunnel edge (which
  terminates TLS) sees only ciphertext.
* **Server auth + channel binding** — the server signs ``"SBv1-chal" ‖ epk_s ‖ epk_c ‖
  nonce_s`` with its static key; the client pin-checks ``server_pubkey`` then verifies the
  signature, so an active MITM cannot substitute its own ephemeral and sit in the middle.
* **Device auth** — the device signs ``"SBv1-auth" ‖ nonce_s ‖ epk_s ‖ epk_c ‖ device_id``;
  bound to the fresh nonce + both ephemerals, so it is un-replayable and un-spliceable.
* **Enrollment** — a one-time pairing code keys an HMAC over the enrollment transcript
  (``verify_enroll_proof``), carried *inside* the already-server-authenticated channel, so
  even a fully active edge that never saw the code can neither read it nor forge the proof
  nor swap the device pubkey. (SPAKE2 is the documented future hardening; the HMAC-behind-
  pin form here is strong because the channel already locks the edge out — see §6.2.)

**This module is imported by both the host and the reference connector**, so the transcript
builders and primitives are shared and the two sides cannot drift on a byte.

Transcripts are built by :func:`_transcript` (domain label + ``0x1f``-separated base64/text
parts) so concatenation is unambiguous and reproduced identically on both ends.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Domain-separation labels — one per signed/MACed context (never reused across contexts).
_LABEL_CHAL = "SBv1-chal"  # /stream server challenge signature
_LABEL_AUTH = "SBv1-auth"  # /stream device auth signature
_LABEL_ENRCHAL = "SBv1-enrchal"  # /enroll server challenge signature
_LABEL_ENROLL = "SBv1-enroll"  # /enroll enrollment transcript (MAC + device sig)
_HKDF_INFO = b"SBv1-channel"  # channel key derivation
_SEP = b"\x1f"  # unit separator between transcript parts

NONCE_BYTES = 32
_AEAD_NONCE_BYTES = 12  # ChaCha20-Poly1305 nonce width


# --------------------------------------------------------------------------------------
# base64 + small helpers
# --------------------------------------------------------------------------------------


def b64e(raw: bytes) -> str:
    """URL-safe-free standard base64 (no newlines), for putting bytes on the JSON wire."""
    return base64.b64encode(raw).decode("ascii")


def b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def new_nonce() -> str:
    """A fresh 32-byte random nonce, base64. Used for challenge freshness/anti-replay."""
    return b64e(os.urandom(NONCE_BYTES))


def _transcript(label: str, *parts: str) -> bytes:
    """Build an unambiguous transcript: ``label ‖ 0x1f ‖ part0 ‖ 0x1f ‖ part1 ‖ …``.

    Parts are ASCII (base64 strings, ids, names). The fixed order + separator means both
    sides reproduce identical bytes with zero risk of field-boundary confusion.
    """
    out = label.encode("ascii")
    for p in parts:
        out += _SEP + p.encode("utf-8")
    return out


# ---- Ed25519 sign / verify -----------------------------------------------------------


def sign(private_key: ed25519.Ed25519PrivateKey, data: bytes) -> str:
    """Ed25519-sign ``data``; return base64 signature."""
    return b64e(private_key.sign(data))


def verify(pubkey_b64: str, sig_b64: str, data: bytes) -> bool:
    """Verify an Ed25519 ``sig_b64`` over ``data`` against a base64 public key."""
    try:
        pub = ed25519.Ed25519PublicKey.from_public_bytes(b64d(pubkey_b64))
        pub.verify(b64d(sig_b64), data)
        return True
    except (InvalidSignature, ValueError):
        return False


def device_id_for_pubkey(device_pubkey_b64: str) -> str:
    """Derive the self-certifying ``device_id`` from a device's public key (§6.1).

    ``base32(sha256(pubkey))[:16]`` (lowercased) — collision-resistant, stable for the
    life of the key, and the same id used for namespacing (§7). Server-assigned at
    enrollment and echoed by the device in ``hello``.
    """
    digest = hashlib.sha256(b64d(device_pubkey_b64)).digest()
    return base64.b32encode(digest).decode("ascii").rstrip("=")[:16].lower()


def fingerprint_for_pubkey(pubkey_raw: bytes) -> str:
    """Short, human-conveyable pin of an Ed25519 public key (§6.1).

    ``base32(sha256(pubkey))[:16]`` grouped as ``XXXX-XXXX-XXXX-XXXX`` (80 bits) — enough
    to defeat collision/preimage for an out-of-band pin, short enough to read aloud.
    """
    digest = hashlib.sha256(pubkey_raw).digest()
    b32 = base64.b32encode(digest).decode("ascii").rstrip("=")[:16]
    return "-".join(b32[i : i + 4] for i in range(0, 16, 4))


# ---- transcript builders (shared by host + connector) --------------------------------


def chal_transcript(epk_s_b64: str, epk_c_b64: str, nonce_s_b64: str) -> bytes:
    """What the server signs (and the client verifies) in the ``/stream`` challenge."""
    return _transcript(_LABEL_CHAL, epk_s_b64, epk_c_b64, nonce_s_b64)


def auth_transcript(nonce_s_b64: str, epk_s_b64: str, epk_c_b64: str, device_id: str) -> bytes:
    """What the device signs (and the server verifies) in the ``/stream`` auth frame."""
    return _transcript(_LABEL_AUTH, nonce_s_b64, epk_s_b64, epk_c_b64, device_id)


def enroll_chal_transcript(epk_s_b64: str, epk_c_b64: str, nonce_b64: str) -> bytes:
    """What the server signs in the ``/enroll`` challenge (the ``/stream`` twin)."""
    return _transcript(_LABEL_ENRCHAL, epk_s_b64, epk_c_b64, nonce_b64)


def enroll_transcript(
    *,
    device_pubkey_b64: str,
    epk_c_b64: str,
    epk_s_b64: str,
    nonce_b64: str,
    device_name: str,
    want: list[str],
) -> bytes:
    """The enrollment transcript both the pairing-code MAC and the device sig cover (§6.2).

    Includes the device pubkey, so a swapped pubkey breaks the MAC; includes both
    ephemerals + nonce, so it is bound to this ``/enroll`` connection.
    """
    return _transcript(
        _LABEL_ENROLL,
        device_pubkey_b64,
        epk_c_b64,
        epk_s_b64,
        nonce_b64,
        device_name,
        ",".join(want),
    )


def enroll_mac(pairing_code: str, transcript: bytes) -> str:
    """HMAC-SHA256 over the enrollment transcript, keyed by the pairing code (base64)."""
    return b64e(hmac.new(pairing_code.encode("utf-8"), transcript, hashlib.sha256).digest())


# --------------------------------------------------------------------------------------
# Value types
# --------------------------------------------------------------------------------------


@dataclass
class ServerIdentity:
    """SB's static server identity (§6.1)."""

    private_key: ed25519.Ed25519PrivateKey | None
    public_key: ed25519.Ed25519PublicKey | None
    fingerprint: str  # short pin shown at `sb pair`, verified by every connector

    @property
    def public_key_b64(self) -> str:
        assert self.public_key is not None
        raw = self.public_key.public_bytes_raw()
        return b64e(raw)


@dataclass
class Challenge:
    """Server-side challenge material for one connection (``/stream`` or ``/enroll``)."""

    epk_s: str  # base64 X25519 server ephemeral public key
    server_pubkey: str  # base64 ed25519 static server public key
    server_sig: str  # base64 ed25519 over the (enroll-)chal transcript
    nonce_s: str  # base64 fresh nonce (the ``nonce_s`` / ``nonce`` wire field)
    _secret: x25519.X25519PrivateKey | None = None  # server ephemeral priv, host-side only


class SessionChannel:
    """Forward-secret AEAD channel over a connection's ECDH session key (§6.3).

    Two directional keys (client→server, server→client) from one HKDF; each direction uses
    an **implicit** monotonic 64-bit counter as the ChaCha20-Poly1305 nonce (not sent on the
    wire — WebSocket is ordered + reliable, so both ends stay in lockstep). Any tamper or
    reorder makes :meth:`open` raise, which the caller maps to an auth failure / close.
    """

    def __init__(self, send_key: bytes, recv_key: bytes) -> None:
        self._send = ChaCha20Poly1305(send_key)
        self._recv = ChaCha20Poly1305(recv_key)
        self._send_ctr = 0
        self._recv_ctr = 0

    def seal(self, plaintext: bytes) -> bytes:
        nonce = self._send_ctr.to_bytes(_AEAD_NONCE_BYTES, "big")
        self._send_ctr += 1
        return self._send.encrypt(nonce, plaintext, None)

    def open(self, ciphertext: bytes) -> bytes:  # noqa: A003 - mirrors builtins.open by design
        nonce = self._recv_ctr.to_bytes(_AEAD_NONCE_BYTES, "big")
        self._recv_ctr += 1
        try:
            return self._recv.decrypt(nonce, ciphertext, None)
        except InvalidTag as exc:  # tamper / desync
            raise ValueError("AEAD open failed (tamper or channel desync)") from exc


# --------------------------------------------------------------------------------------
# Server identity
# --------------------------------------------------------------------------------------


def load_or_create_server_key(
    key_path: str | os.PathLike[str], pub_path: str | os.PathLike[str]
) -> ServerIdentity:
    """Load SB's static server keypair, generating it on first run (§6.1).

    The 32-byte raw Ed25519 private key is written 0600 to ``key_path`` (git-ignored); the
    fingerprint + base64 public key are written to ``pub_path`` (shown at pairing time).
    """
    key_p = Path(key_path)
    if key_p.exists():
        priv = ed25519.Ed25519PrivateKey.from_private_bytes(key_p.read_bytes())
    else:
        priv = ed25519.Ed25519PrivateKey.generate()
        key_p.parent.mkdir(parents=True, exist_ok=True)
        # 0600 from creation: open with restrictive mode before writing the secret.
        fd = os.open(key_p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(priv.private_bytes_raw())
    pub = priv.public_key()
    fp = fingerprint_for_pubkey(pub.public_bytes_raw())
    pub_p = Path(pub_path)
    pub_p.parent.mkdir(parents=True, exist_ok=True)
    pub_p.write_text(f"{fp}\n{b64e(pub.public_bytes_raw())}\n", encoding="utf-8")
    return ServerIdentity(private_key=priv, public_key=pub, fingerprint=fp)


def server_fingerprint(identity: ServerIdentity) -> str:
    """The short fingerprint pinned into connectors out-of-band (§6.1)."""
    return identity.fingerprint


# --------------------------------------------------------------------------------------
# Ephemerals + channel
# --------------------------------------------------------------------------------------


def generate_ephemeral() -> tuple[x25519.X25519PrivateKey, str]:
    """A fresh X25519 ephemeral keypair; returns ``(private, epk_b64)``."""
    priv = x25519.X25519PrivateKey.generate()
    return priv, b64e(priv.public_key().public_bytes_raw())


def derive_channel(
    local_priv: x25519.X25519PrivateKey,
    peer_pub_b64: str,
    nonce_b64: str,
    *,
    is_server: bool,
) -> SessionChannel:
    """ECDH(local, peer) → HKDF-SHA256 → two directional ChaCha20-Poly1305 keys (§6.3).

    ``nonce_b64`` (the server nonce) salts the HKDF for freshness. ``is_server`` selects
    which derived key is send vs recv so the two ends agree.
    """
    peer = x25519.X25519PublicKey.from_public_bytes(b64d(peer_pub_b64))
    shared = local_priv.exchange(peer)
    okm = HKDF(
        algorithm=hashes.SHA256(), length=64, salt=b64d(nonce_b64), info=_HKDF_INFO
    ).derive(shared)
    k_c2s, k_s2c = okm[:32], okm[32:]
    if is_server:
        return SessionChannel(send_key=k_s2c, recv_key=k_c2s)
    return SessionChannel(send_key=k_c2s, recv_key=k_s2c)


# --------------------------------------------------------------------------------------
# Per-connection handshake (§6.3)
# --------------------------------------------------------------------------------------


def make_challenge(identity: ServerIdentity, epk_c: str, *, enroll: bool = False) -> Challenge:
    """Produce a fresh challenge for a new connection (server side, §6.3 / §6.2).

    Generates the server X25519 ephemeral, mints a nonce, and signs the (enroll-)chal
    transcript over ``epk_s ‖ epk_c ‖ nonce`` with the SB static key. ``_secret`` (the
    ephemeral private) is retained host-side for :func:`derive_session_channel`.
    """
    if identity.private_key is None:
        raise RuntimeError("server identity not loaded")
    x_priv, epk_s = generate_ephemeral()
    nonce_s = new_nonce()
    tx = (
        enroll_chal_transcript(epk_s, epk_c, nonce_s)
        if enroll
        else chal_transcript(epk_s, epk_c, nonce_s)
    )
    return Challenge(
        epk_s=epk_s,
        server_pubkey=identity.public_key_b64,
        server_sig=sign(identity.private_key, tx),
        nonce_s=nonce_s,
        _secret=x_priv,
    )


def verify_device_auth(
    *,
    device_pubkey: str,
    device_sig: str,
    nonce_s: str,
    epk_s: str,
    epk_c: str,
    device_id: str,
) -> bool:
    """Verify the ``auth`` frame's device signature (proof of key possession, §6.3)."""
    return verify(device_pubkey, device_sig, auth_transcript(nonce_s, epk_s, epk_c, device_id))


def derive_session_channel(
    *, challenge: Challenge, epk_c: str, is_server: bool = True
) -> SessionChannel:
    """Derive the E2E channel from ECDH(epk_s_priv, epk_c) after ``challenge`` (server, §6.3)."""
    if challenge._secret is None:
        raise RuntimeError("challenge missing server ephemeral secret")
    return derive_channel(challenge._secret, epk_c, challenge.nonce_s, is_server=is_server)


# --------------------------------------------------------------------------------------
# Enrollment proof (§6.2) — pairing-code-keyed MAC + device self-signature
# --------------------------------------------------------------------------------------


def verify_enroll_proof(
    *,
    pairing_code: str,
    mac: str,
    device_sig: str,
    device_pubkey: str,
    transcript: bytes,
) -> bool:
    """Verify an ``enroll_proof`` against one candidate pairing code (§6.2).

    Confirms (constant-time) that ``mac`` is ``HMAC(pairing_code, transcript)`` *and* that
    ``device_sig`` is a valid self-signature by ``device_pubkey`` over the same transcript.
    A swapped pubkey or tampered field breaks one of the two. The caller tries this against
    each active pairing code; a single match registers the device.
    """
    expected_mac = enroll_mac(pairing_code, transcript)
    if not hmac.compare_digest(expected_mac, mac):
        return False
    return verify(device_pubkey, device_sig, transcript)


# --------------------------------------------------------------------------------------
# Frame sealing helpers (convenience wrappers over SessionChannel)
# --------------------------------------------------------------------------------------


def seal(channel: SessionChannel, plaintext: bytes) -> bytes:
    """Seal one outbound frame through the connection's channel (§6.3)."""
    return channel.seal(plaintext)


def open(channel: SessionChannel, ciphertext: bytes) -> bytes:  # noqa: A001 - stable seam name
    """Open one inbound frame through the connection's channel (§6.3)."""
    return channel.open(ciphertext)
