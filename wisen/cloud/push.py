"""
Web Push: the server encrypts a short notification for each browser that
subscribed (the installed dashboard), signed with the server's VAPID key. The
browser's push service only ever sees ciphertext.

Keys come from the environment (WISEN_VAPID_PRIVATE_KEY, WISEN_VAPID_PUBLIC_KEY);
`wisen cloud vapid-keys` makes a pair. Without them push is off and the
dashboard hides its "Enable notifications" button.
"""
import asyncio
import base64
from dataclasses import dataclass
from typing import Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


class Gone(Exception):
    """The push service says this subscription no longer exists (404/410)."""


class PushSender(Protocol):
    async def send(self, subscription: dict, payload: str) -> None:
        """Deliver one notification, or raise Gone if the subscription is dead."""


@dataclass
class WebPushSender:
    private_key: str  # base64url raw P-256 private key
    subject: str      # "mailto:…" or an https URL; push services may contact it

    async def send(self, subscription: dict, payload: str) -> None:
        from pywebpush import WebPushException, webpush

        def deliver() -> None:
            try:
                webpush(subscription, payload, vapid_private_key=self.private_key,
                        vapid_claims={"sub": self.subject}, ttl=24 * 3600, timeout=15)
            except WebPushException as e:
                if e.response is not None and e.response.status_code in (404, 410):
                    raise Gone from None
                raise

        await asyncio.to_thread(deliver)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def generate_vapid_keys() -> tuple[str, str]:
    """(private, public), base64url: the raw private scalar, and the
    uncompressed public point the browser needs as its applicationServerKey."""
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return _b64url(private), _b64url(public)
