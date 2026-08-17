"""voice_models — local Whisper GGML registry, verified download, lookup."""
from __future__ import annotations

import hashlib
import io

import pytest
from maverick import voice_models


def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    for env in ("MAVERICK_WHISPER_MODEL", "MAVERICK_WHISPER_CPP_MODEL",
                "MAVERICK_VOICE_AUTO_FETCH"):
        monkeypatch.delenv(env, raising=False)


class _FakeResp:
    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)

    def read(self, n: int) -> bytes:
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _serve(monkeypatch, payload: bytes):
    seen: list[str] = []

    def _fake_urlopen(url, timeout=0.0):
        seen.append(url)
        return _FakeResp(payload)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    return seen


def _pin(monkeypatch, name: str, payload: bytes):
    fn = voice_models.KNOWN_MODELS[name][0]
    monkeypatch.setitem(
        voice_models.KNOWN_MODELS, name,
        (fn, hashlib.sha256(payload).hexdigest(), len(payload)))


def test_models_dir_under_maverick_home(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    assert voice_models.models_dir() == tmp_path / "models" / "whisper"


def test_model_path_unknown_name_lists_choices(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="unknown Whisper model.*base"):
        voice_models.model_path("bogus")


def test_download_rejects_checksum_mismatch(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    _serve(monkeypatch, b"tampered bytes")
    with pytest.raises(ValueError, match="checksum mismatch"):
        voice_models.download_model("tiny")
    # Nothing installed, no .part litter.
    assert voice_models.installed_models() == []
    assert list(voice_models.models_dir().glob("*")) == []


def test_download_installs_verified_model(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    payload = b"fake ggml weights"
    _pin(monkeypatch, "tiny", payload)
    urls = _serve(monkeypatch, payload)
    ticks: list[tuple[int, int]] = []
    path = voice_models.download_model(
        "tiny", progress=lambda done, total: ticks.append((done, total)))
    assert path.read_bytes() == payload
    assert path.name == "ggml-tiny.bin"
    assert urls == [voice_models._BASE_URL + "ggml-tiny.bin"]
    assert ticks and ticks[-1] == (len(payload), len(payload))
    assert voice_models.verify_model(path) is True


def test_download_skips_existing_unless_force(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    dest = voice_models.model_path("tiny")
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"already here")

    def _boom(url, timeout=0.0):
        raise AssertionError("network hit despite existing file")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    assert voice_models.download_model("tiny") == dest

    payload = b"fresh copy"
    _pin(monkeypatch, "tiny", payload)
    _serve(monkeypatch, payload)
    assert voice_models.download_model("tiny", force=True).read_bytes() == payload


def test_verify_model_flags_pinned_mismatch_and_allows_foreign(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    d = voice_models.models_dir()
    d.mkdir(parents=True)
    bad = d / "ggml-tiny.bin"
    bad.write_bytes(b"wrong")
    assert voice_models.verify_model(bad) is False
    foreign = d / "ggml-custom-finetune.bin"
    foreign.write_bytes(b"operator supplied")
    assert voice_models.verify_model(foreign) is None


def test_locate_model_env_path_wins(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    explicit = tmp_path / "my-model.bin"
    explicit.write_bytes(b"x")
    monkeypatch.setenv("MAVERICK_WHISPER_CPP_MODEL", str(explicit))
    assert voice_models.locate_model() == explicit


def test_locate_model_uses_any_installed_ggml(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    d = voice_models.models_dir()
    d.mkdir(parents=True)
    hand_copied = d / "ggml-small.en.bin"
    hand_copied.write_bytes(b"x")
    # Configured size (default 'base') absent -> the hand-copied file works.
    assert voice_models.locate_model() == hand_copied


def test_locate_model_none_when_auto_fetch_disabled(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    monkeypatch.setenv("MAVERICK_VOICE_AUTO_FETCH", "0")

    def _boom(url, timeout=0.0):
        raise AssertionError("auto-fetch disabled; must not touch the network")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    assert voice_models.locate_model() is None


def test_auto_fetch_defaults_on_for_standard_deployments(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    assert voice_models.auto_fetch_enabled() is True


def test_auto_fetch_defaults_off_under_egress_lock(monkeypatch, tmp_path):
    """An egress-locked box must not phone Hugging Face because someone
    clicked a mic — the enterprise boundary flips the default."""
    _home(monkeypatch, tmp_path)
    import maverick.enterprise as enterprise
    monkeypatch.setattr(enterprise, "enterprise_enabled", lambda: True)
    assert voice_models.auto_fetch_enabled() is False


def test_auto_fetch_explicit_config_beats_posture(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    import maverick.enterprise as enterprise
    (tmp_path / "config.toml").write_text(
        "[voice]\nauto_fetch_model = true\n", encoding="utf-8")
    monkeypatch.setattr(enterprise, "enterprise_enabled", lambda: True)
    assert voice_models.auto_fetch_enabled() is True
    (tmp_path / "config.toml").write_text(
        "[voice]\nauto_fetch_model = false\n", encoding="utf-8")
    monkeypatch.setattr(enterprise, "enterprise_enabled", lambda: False)
    assert voice_models.auto_fetch_enabled() is False


def test_auto_fetch_env_beats_everything(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    (tmp_path / "config.toml").write_text(
        "[voice]\nauto_fetch_model = true\n", encoding="utf-8")
    monkeypatch.setenv("MAVERICK_VOICE_AUTO_FETCH", "0")
    assert voice_models.auto_fetch_enabled() is False


def test_locate_model_auto_fetch_downloads_configured(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    payload = b"auto fetched weights"
    _pin(monkeypatch, "base", payload)
    _serve(monkeypatch, payload)
    monkeypatch.setenv("MAVERICK_VOICE_AUTO_FETCH", "1")
    got = voice_models.locate_model()
    assert got is not None and got.name == "ggml-base.bin"
    assert got.read_bytes() == payload


def test_configured_model_env_beats_default(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    assert voice_models.configured_model() == "base"
    monkeypatch.setenv("MAVERICK_WHISPER_MODEL", "small.en")
    assert voice_models.configured_model() == "small.en"


# ---- maverick voice CLI ------------------------------------------------------






