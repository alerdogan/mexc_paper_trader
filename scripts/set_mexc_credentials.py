#!/usr/bin/env python3
"""Interactively and atomically write the root-owned systemd credential file."""

import getpass
import os
import stat
import tempfile
from pathlib import Path


SECRET_DIR = Path("/etc/mexc-trader")
SECRET_FILE = SECRET_DIR / "secrets.env"


def validate_value(name, value):
    if not value:
        raise ValueError(f"{name} cannot be empty")
    if any(character in value for character in "\r\n\0"):
        raise ValueError(f"{name} cannot contain line breaks or NUL")


def environment_quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def validate_secret_directory(directory):
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise RuntimeError("Secret directory must be a real directory")
    if info.st_uid != 0 or info.st_gid != 0:
        raise RuntimeError("Secret directory must be owned by root:root")
    if stat.S_IMODE(info.st_mode) & 0o027:
        raise RuntimeError("Secret directory must not be group-writable or accessible to others")


def atomic_write_credentials(path, key, secret, owner_uid=0, owner_gid=0):
    validate_value("MEXC_API_KEY", key)
    validate_value("MEXC_API_SECRET", secret)
    validate_secret_directory(path.parent)

    try:
        current = os.lstat(path)
    except FileNotFoundError:
        current = None
    if current and (stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode)):
        raise RuntimeError("Credential target must be a regular file, never a symlink")

    content = (
        f"MEXC_API_KEY={environment_quote(key)}\n"
        f"MEXC_API_SECRET={environment_quote(secret)}\n"
    )
    fd, temporary_name = tempfile.mkstemp(prefix=".secrets.env.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        os.fchown(fd, owner_uid, owner_gid)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600, follow_symlinks=False)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run this script with sudo; credentials must remain root-owned.")
    validate_secret_directory(SECRET_DIR)
    key = getpass.getpass("MEXC API Key: ")
    secret = getpass.getpass("MEXC API Secret: ")
    try:
        atomic_write_credentials(SECRET_FILE, key, secret)
    finally:
        key = ""
        secret = ""
    print("Credential file securely updated; values were not displayed.")


if __name__ == "__main__":
    main()
