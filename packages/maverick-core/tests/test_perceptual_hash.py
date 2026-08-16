"""Tests for maverick.perceptual_hash (8x8 average-hash + hamming).

The load-bearing assertion: Python and the Rust twin
(rust/mvk-scan/src/phash.rs) hash the shared synthetic gradient to the SAME
constant. Both sides assert GRADIENT_HASH; the Rust side does it in its own
`cargo test`, so this file pins the Python half of that contract.
"""
from __future__ import annotations

import sys

import pytest
from maverick.perceptual_hash import (
    GRADIENT_HASH,
    average_hash_file,
    average_hash_from_pixels,
    hamming,
    synthetic_gradient,
)


def test_gradient_matches_shared_constant():
    assert average_hash_from_pixels(synthetic_gradient(), 64, 64) == GRADIENT_HASH


def test_flat_image_hashes_to_zero():
    # Strict ">" means a perfectly uniform image (all ties) is all zeros.
    assert average_hash_from_pixels([(128, 128, 128)] * 64 * 64, 64, 64) == "0" * 16


def test_inverted_image_is_bitwise_complement():
    inv = [(255 - r, 255 - g, 255 - b) for (r, g, b) in synthetic_gradient()]
    h = average_hash_from_pixels(inv, 64, 64)
    assert hamming(GRADIENT_HASH, h) == 64


def test_hash_stable_under_small_noise():
    """A near-duplicate (tiny brightness wobble) stays within a few bits."""
    wobbled = [
        (min(255, r + (i % 3)), g, b)
        for i, (r, g, b) in enumerate(synthetic_gradient())
    ]
    h = average_hash_from_pixels(wobbled, 64, 64)
    assert hamming(GRADIENT_HASH, h) <= 5


def test_non_square_image_supported():
    px = [(x % 256, y % 256, 0) for y in range(16) for x in range(40)]
    h = average_hash_from_pixels(px, 40, 16)
    assert len(h) == 16 and int(h, 16) >= 0


def test_too_small_image_rejected():
    with pytest.raises(ValueError, match="at least 8x8"):
        average_hash_from_pixels([(0, 0, 0)] * 49, 7, 7)


def test_pixel_count_mismatch_rejected():
    with pytest.raises(ValueError, match="expected"):
        average_hash_from_pixels([(0, 0, 0)] * 10, 64, 64)


def test_hamming_validates_and_measures():
    assert hamming(GRADIENT_HASH, GRADIENT_HASH) == 0
    assert hamming("0" * 16, "f" * 16) == 64
    with pytest.raises(ValueError):
        hamming("abc", GRADIENT_HASH)


def test_file_hash_without_pillow_is_actionable(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "PIL", None)  # forces ImportError
    with pytest.raises(ImportError, match=r"packages/maverick-core\[computer-use\]"):
        average_hash_file(str(tmp_path / "x.png"))


def test_file_hash_matches_pixels_when_pillow_present(tmp_path):
    Image = pytest.importorskip("PIL.Image", reason="Pillow (computer-use extra) not installed")
    im = Image.new("RGB", (64, 64))
    im.putdata(synthetic_gradient())
    p = tmp_path / "gradient.png"
    im.save(p)
    assert average_hash_file(str(p)) == GRADIENT_HASH
