import asyncio
import json
import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from scripts import set_mexc_credentials


PLACEHOLDER_KEY = "PLACEHOLDER_KEY_ONLY"
PLACEHOLDER_SECRET = "PLACEHOLDER_SECRET_ONLY"


def test_linux_without_environment_does_not_call_keychain(
    isolated_app, monkeypatch
):
    module, _ = isolated_app
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(
        module,
        "keychain_get",
        lambda account: (_ for _ in ()).throw(
            AssertionError("Linux must not call macOS Keychain")
        ),
    )

    assert module.credential_get("api_key") == ""
    assert module.credential_get("api_secret") == ""
    assert module.credentials_available() is False


def test_linux_reads_credentials_from_environment(isolated_app, monkeypatch):
    module, _ = isolated_app
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setenv("MEXC_API_KEY", PLACEHOLDER_KEY)
    monkeypatch.setenv("MEXC_API_SECRET", PLACEHOLDER_SECRET)

    assert module.credential_get("api_key") == PLACEHOLDER_KEY
    assert module.credential_get("api_secret") == PLACEHOLDER_SECRET
    assert module.credentials_available() is True


def test_macos_keeps_keychain_fallback_and_environment_precedence(
    isolated_app, monkeypatch
):
    module, _ = isolated_app
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module, "keychain_get", lambda account: "KEYCHAIN_PLACEHOLDER")

    assert module.credential_get("api_key") == "KEYCHAIN_PLACEHOLDER"
    monkeypatch.setenv("MEXC_API_KEY", PLACEHOLDER_KEY)
    assert module.credential_get("api_key") == PLACEHOLDER_KEY


def test_private_test_is_safely_unavailable_without_credentials(
    isolated_app, monkeypatch
):
    module, _ = isolated_app
    monkeypatch.setattr(module.sys, "platform", "linux")

    with pytest.raises(HTTPException) as raised:
        asyncio.run(module.private_test())
    assert raised.value.status_code == 503
    assert "PAPER" in raised.value.detail


def test_linux_web_credential_write_is_unavailable(isolated_app, monkeypatch):
    module, _ = isolated_app
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(
        module,
        "keychain_set",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("Linux must not write macOS Keychain")
        ),
    )

    with pytest.raises(HTTPException) as raised:
        module.api_settings(
            module.ApiCfg(
                api_key=PLACEHOLDER_KEY,
                api_secret=PLACEHOLDER_SECRET,
            )
        )
    assert raised.value.status_code == 503


def test_redaction_removes_credentials_from_errors(isolated_app):
    module, _ = isolated_app
    source = f"request {PLACEHOLDER_KEY} failed with {PLACEHOLDER_SECRET}"
    result = module.redact_credentials(
        source, PLACEHOLDER_KEY, PLACEHOLDER_SECRET
    )
    assert PLACEHOLDER_KEY not in result
    assert PLACEHOLDER_SECRET not in result
    assert result.count("[REDACTED]") == 2


def test_private_test_redacts_upstream_response(isolated_app, monkeypatch):
    module, _ = isolated_app
    monkeypatch.setenv("MEXC_API_KEY", PLACEHOLDER_KEY)
    monkeypatch.setenv("MEXC_API_SECRET", PLACEHOLDER_SECRET)

    class Response:
        status_code = 400
        headers = {"content-type": "application/json"}
        text = ""

        def json(self):
            return {
                "message": f"rejected {PLACEHOLDER_KEY} {PLACEHOLDER_SECRET}"
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    async def fake_mexc_get(client, url, **kwargs):
        return Response()

    monkeypatch.setattr(module.httpx, "AsyncClient", Client)
    monkeypatch.setattr(module, "mexc_get", fake_mexc_get)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(module.private_test())
    assert PLACEHOLDER_KEY not in raised.value.detail
    assert PLACEHOLDER_SECRET not in raised.value.detail
    assert raised.value.detail.count("[REDACTED]") == 2


def test_health_never_exposes_environment_credentials(
    isolated_app, monkeypatch
):
    module, _ = isolated_app
    monkeypatch.setenv("MEXC_API_KEY", PLACEHOLDER_KEY)
    monkeypatch.setenv("MEXC_API_SECRET", PLACEHOLDER_SECRET)

    payload = json.dumps(module.health())
    assert PLACEHOLDER_KEY not in payload
    assert PLACEHOLDER_SECRET not in payload


def test_status_never_exposes_environment_credentials(
    isolated_app, monkeypatch
):
    module, _ = isolated_app
    monkeypatch.setenv("MEXC_API_KEY", PLACEHOLDER_KEY)
    monkeypatch.setenv("MEXC_API_SECRET", PLACEHOLDER_SECRET)

    payload = json.dumps(module.status())
    assert PLACEHOLDER_KEY not in payload
    assert PLACEHOLDER_SECRET not in payload


def test_credential_writer_rejects_line_breaks_and_nul():
    for invalid in ("PLACEHOLDER\nVALUE", "PLACEHOLDER\rVALUE", "PLACEHOLDER\0VALUE"):
        with pytest.raises(ValueError):
            set_mexc_credentials.validate_value("credential", invalid)


def test_credential_writer_is_atomic_root_owned_and_mode_0600(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(set_mexc_credentials, "validate_secret_directory", lambda path: None)
    target = tmp_path / "secrets.env"
    set_mexc_credentials.atomic_write_credentials(
        target,
        PLACEHOLDER_KEY,
        PLACEHOLDER_SECRET,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    assert target.is_file()
    assert target.stat().st_mode & 0o777 == 0o600
    content = target.read_text()
    assert f'MEXC_API_KEY="{PLACEHOLDER_KEY}"' in content
    assert f'MEXC_API_SECRET="{PLACEHOLDER_SECRET}"' in content


def test_credential_writer_rejects_symlink_target(tmp_path, monkeypatch):
    monkeypatch.setattr(set_mexc_credentials, "validate_secret_directory", lambda path: None)
    victim = tmp_path / "victim"
    victim.write_text("unchanged")
    target = tmp_path / "secrets.env"
    target.symlink_to(victim)

    with pytest.raises(RuntimeError, match="symlink"):
        set_mexc_credentials.atomic_write_credentials(
            target,
            PLACEHOLDER_KEY,
            PLACEHOLDER_SECRET,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
    assert victim.read_text() == "unchanged"
