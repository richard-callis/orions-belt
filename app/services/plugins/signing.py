"""
Orion's Belt — Plugin Digital Signatures (Ed25519)

Opt-in plugin signing: plugins with a valid .plugin.sig sidecar are
verified before loading. Plugins without a signature pass through
(unrestricted). This is backward-compatible.

Key pair stored in .plugin_signing_key (mode 0600).
"""
import hashlib
import logging
import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

log = logging.getLogger("orions_belt.plugins.signing")

_SIGNING_KEY_DIR = Path(__file__).parent.parent.parent.parent / ".plugin_signing_key"


def _get_key_pair() -> tuple[Ed25519PrivateKey | None, Ed25519PublicKey]:
    """Load or generate the Ed25519 key pair. Stored in .plugin_signing_key/."""
    _SIGNING_KEY_DIR.mkdir(exist_ok=True, parents=True)
    private_key_path = _SIGNING_KEY_DIR / "private.pem"
    public_key_path = _SIGNING_KEY_DIR / "public.pem"

    private_key: Ed25519PrivateKey | None = None
    public_key: Ed25519PublicKey | None = None

    if private_key_path.exists():
        try:
            with open(private_key_path, "rb") as f:
                private_key = serialization.load_pem_private_key(
                    f.read(), password=None
                )
        except Exception as e:
            log.warning("Failed to load existing private key: %s", e)

    if public_key_path.exists():
        try:
            with open(public_key_path, "rb") as f:
                public_key = serialization.load_pem_public_key(f.read())
        except Exception as e:
            log.warning("Failed to load existing public key: %s", e)

    if private_key is None:
        private_key = Ed25519PrivateKey.generate()
        with open(private_key_path, "wb") as f:
            f.write(private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ))
        os.chmod(private_key_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        log.info("Generated new plugin signing key pair")

    if public_key is None:
        public_key = private_key.public_key()
        with open(public_key_path, "wb") as f:
            f.write(public_key.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            ))
        os.chmod(public_key_path, stat.S_IRUSR)  # 0444

    return private_key, public_key


def sign_plugin(plugin_path: Path) -> Path:
    """Sign a plugin file, writing .plugin.sig sidecar.

    Uses SHA-256 of the file content signed with Ed25519.
    Returns the path to the .sig file.
    """
    private_key, _ = _get_key_pair()
    data = plugin_path.read_bytes()
    signature = private_key.sign(hashlib.sha256(data).digest())

    sig_path = plugin_path.with_suffix(plugin_path.suffix + ".sig")
    sig_path.write_bytes(signature)
    log.info("Signed plugin: %s -> %s", plugin_path, sig_path)
    return sig_path


def verify_plugin(plugin_path: Path) -> bool:
    """Verify a plugin's .plugin.sig signature.

    Returns True if:
    - No .plugin.sig sidecar exists (opt-in model — unsigned passes)
    - Signature is valid
    Returns False if signature exists but doesn't match.
    """
    sig_path = plugin_path.with_suffix(plugin_path.suffix + ".sig")

    if not sig_path.exists():
        # No signature — allowed in opt-in model
        return True

    try:
        public_key_path = _SIGNING_KEY_DIR / "public.pem"
        if not public_key_path.exists():
            log.warning("No public key found for signature verification")
            return True  # can't verify, allow through

        with open(public_key_path, "rb") as f:
            public_key = serialization.load_pem_public_key(f.read())

        data = plugin_path.read_bytes()
        signature = sig_path.read_bytes()

        public_key.verify(signature, hashlib.sha256(data).digest())
        log.info("Plugin signature verified: %s", plugin_path)
        return True

    except Exception as e:
        log.error("Plugin signature verification failed for %s: %s", plugin_path, e)
        return False
