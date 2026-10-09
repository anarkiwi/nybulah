import ctypes

import pytest

from nybulah.opencbm import OpenCBM, OpenCBMError


class FakeLib:
    """libopencbm stand-in returning configured codes."""

    def __init__(self, **rc):
        self.rc = rc
        self.calls = []

    def __getattr__(self, name):
        def fn(*args):
            self.calls.append(name)
            if name == "cbm_device_status":
                args[2].value = b"73,CBM DOS V2.6 1541,00,00\r"
            if name == "cbm_identify":
                args[2]._obj.value = 0  # pylint: disable=protected-access
            return self.rc.get(name, 0)

        return fn


def test_short_transfers_raise():
    cbm = OpenCBM(lib=FakeLib(cbm_download=1, cbm_upload=2))
    with pytest.raises(OpenCBMError, match="cbm_download returned 1"):
        cbm.download(8, 0x8000, 2)
    cbm.upload(8, 0x8000, b"ab")
    with pytest.raises(OpenCBMError):
        cbm.upload(8, 0x8000, b"abc")


def test_status_identify_and_lines():
    lib = FakeLib(cbm_iec_poll=3)
    with OpenCBM(lib=lib) as cbm:
        assert cbm.status(8) == "73,CBM DOS V2.6 1541,00,00"
        assert cbm.identify(8) == (0, "")
        assert cbm.iec_poll() == 3
        cbm.iec_set(1)
        cbm.iec_release(1)
        cbm.reset()
        cbm.command(8, b"I")
    assert lib.calls[-1] == "cbm_driver_close" and cbm.fd is None


def test_open_and_plugin_failures():
    with pytest.raises(OpenCBMError):
        OpenCBM(lib=FakeLib(cbm_driver_open_ex=-1))
    cbm = OpenCBM(lib=FakeLib(cbm_get_plugin_function_address=None, cbm_reset=-1))
    with pytest.raises(OpenCBMError, match="plugin lacks"):
        cbm.s1_read(1)
    with pytest.raises(OpenCBMError, match="cbm_reset"):
        cbm.reset()
    assert isinstance(cbm.fd, ctypes.c_ssize_t)
