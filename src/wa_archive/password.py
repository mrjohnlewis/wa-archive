"""Backup password: macOS Keychain (opt-in) or an interactive prompt.

The password is never accepted via argv or environment, and never logged.
"""

from __future__ import annotations

import getpass

import keyring
import keyring.errors

SERVICE = "wa-archive"
ACCOUNT = "ios-backup-password"


def saved_password() -> str | None:
    try:
        return keyring.get_password(SERVICE, ACCOUNT)
    except keyring.errors.KeyringError:
        return None


def get_password() -> str:
    pw = saved_password()
    if pw:
        return pw
    return getpass.getpass("iPhone backup password (Finder encryption password): ")


def save_password() -> None:
    pw = getpass.getpass("iPhone backup password to save in Keychain: ")
    confirm = getpass.getpass("Again to confirm: ")
    if pw != confirm:
        raise SystemExit("Passwords did not match; nothing saved.")
    keyring.set_password(SERVICE, ACCOUNT, pw)


def forget_password() -> bool:
    try:
        keyring.delete_password(SERVICE, ACCOUNT)
        return True
    except keyring.errors.PasswordDeleteError:
        return False
