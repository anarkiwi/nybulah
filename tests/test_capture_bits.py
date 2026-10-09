import numpy as np

from nybulah.analysis import capture_bits, to_bits, trailing_ones


def test_trailing_ones():
    bits = to_bits(np.array([0x0F, 0xFF, 0xF0], np.uint8))
    assert trailing_ones(bits, [0, 4, 8, 16, 20, 24]).tolist() == [0, 0, 4, 12, 16, 0]


def test_restores_hidden_sync_ones():
    data = np.array([0x55, 0xFF, 0x52, 0x55, 0x52], np.uint8)
    bits = capture_bits(data, [2, 4], [41, 12], lead=10)
    want = np.concatenate(
        (
            np.ones(10, np.uint8),
            to_bits(data[:2]),
            np.ones(41 - 9, np.uint8),
            to_bits(data[2:4]),
            np.ones(12 - 1, np.uint8),
            to_bits(data[4:]),
        )
    )
    assert (bits == want).all()


def test_short_runs_become_hardware_syncs():
    data = np.array([0x00, 0x52], np.uint8)
    bits = capture_bits(data, [1], [4])
    assert len(bits) == 16 + 10 and bits[8:18].all()
