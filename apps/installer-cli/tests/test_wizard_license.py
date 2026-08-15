"""The wizard's licensing step writes ``[license]``, and the kernel reads it.

Rule-6 integrity: a wizard toggle must actually reach the feature. Here the
entitlement gate (``maverick.entitlements``) consumes exactly the ``[license]``
table the wizard writes. Default (no enforcement) writes no section, so the
kernel stays fail-open and runs every feature.
"""
from __future__ import annotations

try:
    import tomllib  # 3.11+
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


def _write(cfg_dir, monkeypatch, license_cfg):
    monkeypatch.setattr("maverick_installer.wizard.CONFIG_DIR", cfg_dir)
    monkeypatch.setattr("maverick_installer.wizard.ENV_FILE", cfg_dir / ".env")
    monkeypatch.setattr("maverick_installer.wizard.CONFIG_FILE", cfg_dir / "config.toml")
    from maverick_installer.wizard import write_config
    write_config(
        providers=["anthropic"], role_models={},
        channels={}, safety={"profile": "balanced"},
        budget={"max_dollars": 5.0, "max_wall_seconds": 600, "max_tool_calls": 30},
        sandbox={"backend": "local", "workdir": "~/ws"},
        keys={"ANTHROPIC_API_KEY": "x"},
        license_cfg=license_cfg,
    )
    return (cfg_dir / "config.toml").read_text()


def test_default_writes_no_license_section(tmp_path, monkeypatch):
    # No enforcement -> no [license] table -> the kernel runs fail-open.
    assert "[license]" not in _write(tmp_path, monkeypatch, None)


def test_license_choices_are_written(tmp_path, monkeypatch):
    cfg = _write(tmp_path, monkeypatch, {
        "enforce": True,
        "publisher_pubkeys": ["aa" * 32],
        "api_url": "https://api.example/license",
        "api_token": "${MAVERICK_LICENSE_API_TOKEN}",
        "refresh_interval_seconds": 60,
    })
    parsed = tomllib.loads(cfg)["license"]
    assert parsed["enforce"] is True
    assert parsed["publisher_pubkeys"] == ["aa" * 32]
    assert parsed["api_url"] == "https://api.example/license"
    # The secret is an env reference, not an inline value.
    assert parsed["api_token"] == "${MAVERICK_LICENSE_API_TOKEN}"
    assert parsed["refresh_interval_seconds"] == 60


def test_refresh_interval_reaches_the_kernel(tmp_path, monkeypatch):
    # Rule-6 integrity: the wizard's interval is exactly what the refresher polls.
    cfg = _write(tmp_path, monkeypatch, {
        "enforce": True, "api_url": "https://api.example/license",
        "refresh_interval_seconds": 120,
    })
    parsed = tomllib.loads(cfg)
    from maverick import entitlements as E
    monkeypatch.setattr(E, "_license_cfg", lambda: dict(parsed["license"]))
    monkeypatch.delenv("MAVERICK_LICENSE_API", raising=False)
    monkeypatch.delenv("MAVERICK_LICENSE_REFRESH_INTERVAL", raising=False)
    assert E.refresh_interval_seconds() == 120.0


def test_entitlements_reads_what_the_wizard_writes(tmp_path, monkeypatch):
    cfg = _write(tmp_path, monkeypatch, {"enforce": True,
                                         "publisher_pubkeys": ["bb" * 32]})
    parsed = tomllib.loads(cfg)
    from maverick import entitlements as E
    # The kernel's [license] consumers see exactly the wizard's table.
    monkeypatch.setattr(E, "_license_cfg", lambda: dict(parsed["license"]))
    monkeypatch.delenv("MAVERICK_LICENSE_ENFORCE", raising=False)
    monkeypatch.delenv("MAVERICK_LICENSE_PUBKEYS", raising=False)
    assert E.enforcing() is True
    assert E._trusted_pubkeys() == ["bb" * 32]
