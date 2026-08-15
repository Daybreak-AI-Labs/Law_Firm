"""Regression tests for bug-hunt wave-5 fixes."""
from __future__ import annotations

import os
import stat

import pytest


class TestSecretsURLToken:
    def test_query_string_credential_redacted(self):
        from maverick.secrets import scrub
        out = scrub("GET https://api.x.com/cb?access_token=abc123secret&page=2")
        assert "abc123secret" not in out
        assert "[REDACTED:url_secret]" in out
        # Non-secret params are preserved.
        assert "page=2" in out

    def test_presigned_sig_redacted(self):
        from maverick.secrets import scrub
        out = scrub("https://s3/obj?X=1&sig=DEADBEEFsignature123")
        assert "DEADBEEFsignature123" not in out


class TestAuditKeyPerms:
    @pytest.mark.skipif(os.name != "posix", reason="POSIX perms")
    def test_private_key_created_0600(self, monkeypatch, tmp_path):
        from maverick.audit import signing
        monkeypatch.setattr(signing, "KEY_DIR", tmp_path / "keys")
        # Real Ed25519 material is exactly 32 bytes and the key id is a 16-char
        # lowercase-hex fingerprint; _save_keypair validates both.
        priv_path = signing._save_keypair(b"P" * 32, b"K" * 32, "0123456789abcdef")
        mode = stat.S_IMODE(priv_path.stat().st_mode)
        assert mode == 0o600, oct(mode)

    def test_private_key_bytes_are_never_newline_translated(self, monkeypatch, tmp_path):
        """Raw Ed25519 material must remain exactly 32 bytes on Windows."""
        from maverick.audit import signing

        monkeypatch.setattr(signing, "KEY_DIR", tmp_path / "keys")
        private = (b"A" * 10) + b"\n" + (b"B" * 21)

        priv_path = signing._save_keypair(
            private, b"P" * 32, "0123456789abcdef"
        )

        assert len(private) == 32
        assert priv_path.read_bytes() == private


class TestWorldDBPerms:
    @pytest.mark.skipif(os.name != "posix", reason="POSIX perms")
    def test_db_file_created_0600(self, tmp_path):
        from maverick.world_model import open_world
        db = tmp_path / "world.db"
        w = open_world(db)
        try:
            mode = stat.S_IMODE(db.stat().st_mode)
            assert mode == 0o600, oct(mode)
        finally:
            w.close()
