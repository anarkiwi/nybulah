import numpy as np

from nybulah import passes, unstable
from nybulah.analysis.gcr import encode, to_bits


def gcr_with_noise(start, length, seed=4):
    """GCR bytes of random data with ``length`` bytes of zero-heavy noise at
    ``start``."""
    rng = np.random.default_rng(seed)
    data = encode(rng.integers(0, 256, 4000, dtype=np.uint8))
    data[start : start + length] = rng.integers(0, 256, length) & rng.integers(
        0, 256, length
    )
    return data


def test_contexts_hold_the_bits_before_with_ones_first():
    bits = np.array([0, 0, 1, 0, 1, 1])
    assert unstable.contexts(bits).tolist() == [3, 2, 0, 1, 2, 1]


def test_noise_runs_cover_only_the_noise():
    data = gcr_with_noise(2000, 40)
    first, end = unstable.noise_runs(to_bits(data))
    assert len(first) == 1
    assert abs(first[0] - 8 * 2000) <= 8 and abs(end[0] - 8 * 2040) <= 8
    clean = encode(np.random.default_rng(5).integers(0, 256, 4000, dtype=np.uint8))
    assert unstable.noise_runs(to_bits(clean))[0].size == 0
    assert unstable.noise_runs(np.zeros(0, np.int64))[0].size == 0


def test_noise_posterior_is_certain_on_zeros_gcr_cannot_write():
    bits = to_bits(gcr_with_noise(2000, 40)).astype(np.int64)
    illegal = (bits == 0) & (unstable.contexts(bits) == 0)
    post = unstable.noise_posterior(bits)
    assert illegal.any() and (post[illegal] == 1.0).all()
    assert np.median(post[: 8 * 1900]) < 0.5


def test_absorbable_counts_the_run_and_the_interrupted_byte():
    data = gcr_with_noise(2000, 40)
    data[2039] = 0x0F
    data[2040] = 0x52
    _, latched = passes.capable(data)
    absorb, noisy = unstable.absorbable(data, latched)
    assert noisy[2001:2039].all() and not noisy[:1990].any()
    assert abs(absorb[2040] - 41) <= 1
    assert not absorb[:1990].any() and not absorb[2100:].any()
    assert not unstable.absorbable(data[:0], latched[:0])[0].size


def test_smooth_compiled_and_python_agree():
    bits = to_bits(gcr_with_noise(100, 20)[:200]).astype(np.int64)
    ctx = unstable.contexts(bits)
    gcr = np.array([1.0, 0.6, 0.4, 0.5])
    args = (bits, ctx, gcr, 0.3, np.array([0.01, 0.02]))
    smooth = unstable._smooth  # pylint: disable=protected-access
    fast, slow = smooth(*args), smooth.py_func(*args)
    for a, b in zip(fast, slow):
        assert np.allclose(a, b)
