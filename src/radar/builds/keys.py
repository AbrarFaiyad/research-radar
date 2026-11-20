"""The Anthropic API key for daily builds, kept in the OS credential store (Windows Credential
Manager), never in the repository, config files, or the panel."""

from __future__ import annotations

KEYRING_SERVICE = "research-radar-anthropic"
KEYRING_USER = "default"


def get_key() -> str | None:
    import keyring

    return keyring.get_password(KEYRING_SERVICE, KEYRING_USER) or None


def set_key(key: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)
