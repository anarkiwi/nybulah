import ctypes

import pytest

from nybulah import opencbm
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


class PluginLib(FakeLib):
    """FakeLib whose plugin exports real callbacks for the X entry points."""

    def __init__(self, x_rc=0, xb_rc=0, **rc):
        super().__init__(**rc)
        self.seen = []
        xfer = ctypes.CFUNCTYPE(
            ctypes.c_int, ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint
        )
        tmo = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_ssize_t, ctypes.c_uint)
        write = xfer(lambda fd, buf, n: self.seen.append(ctypes.string_at(buf, n)) or n)
        self.fns = {
            b"opencbm_plugin_x_read_n": xfer(lambda fd, buf, n: x_rc or n),
            b"opencbm_plugin_x_write_n": write,
            b"opencbm_plugin_xb_read_n": xfer(lambda fd, buf, n: xb_rc or n),
            b"opencbm_plugin_xb2_read_n": xfer(lambda fd, buf, n: xb_rc or n),
            b"opencbm_plugin_xb_write_n": write,
            b"opencbm_plugin_xb2_write_n": write,
            b"opencbm_plugin_xum1541_set_timeout": tmo(
                lambda fd, ms: self.seen.append(ms) or 0
            ),
        }

    def cbm_get_plugin_function_address(self, name):
        fn = self.fns.get(name)
        return ctypes.cast(fn, ctypes.c_void_p).value if fn else None


def test_s3_entry_points_and_probe():
    cbm = OpenCBM(lib=PluginLib())
    assert cbm.supports("s3") and cbm.supports("s1") and not cbm.supports("pp")
    assert cbm.s3_read(3) == b"\0\0\0"
    cbm.s3_write(b"Q")
    assert cbm.set_timeout(1500)
    assert cbm.lib.seen == [b"Q", 1500]
    old = OpenCBM(lib=PluginLib(x_rc=-1))
    assert not old.supports("s3")
    bare = OpenCBM(lib=FakeLib(cbm_get_plugin_function_address=None))
    assert not bare.supports("s3") and not bare.set_timeout(1)


def test_xb_entry_points_and_probe():
    cbm = OpenCBM(lib=PluginLib())
    assert cbm.supports("xb") and cbm.supports("s3")
    assert cbm.xb_read(2) == cbm.xb2_read(2) == b"\0\0"
    cbm.xb_write(b"ab")
    cbm.xb2_write(b"cd")
    assert cbm.lib.seen == [b"ab", b"cd"]
    assert not OpenCBM(lib=PluginLib(xb_rc=-1)).supports("xb")
    bare = OpenCBM(lib=FakeLib(cbm_get_plugin_function_address=None))
    assert not bare.supports("xb")


def test_talk_open_and_raw_read():
    lib = FakeLib(cbm_raw_read=3, cbm_get_eoi=1)
    cbm = OpenCBM(lib=lib)
    cbm.open_file(8, 0, b"$")
    cbm.talk(8, 0)
    assert cbm.raw_read(256) == b"\0\0\0" and cbm.get_eoi()
    cbm.untalk()
    cbm.close_file(8, 0)
    assert lib.calls[-6:] == [
        "cbm_open",
        "cbm_talk",
        "cbm_raw_read",
        "cbm_get_eoi",
        "cbm_untalk",
        "cbm_close",
    ]
    bad = OpenCBM(lib=FakeLib(cbm_talk=1, cbm_open=-1, cbm_raw_read=-1))
    for fn, args in ((bad.talk, (8, 15)), (bad.open_file, (8, 0, b"$"))):
        with pytest.raises(OpenCBMError):
            fn(*args)
    with pytest.raises(OpenCBMError, match="cbm_raw_read returned -1"):
        bad.raw_read(4)


def usb_tree(root, *devices):
    """A sysfs USB device directory with (name, vid, pid, bus, dev) entries."""
    for name, vid, pid, busnum, devnum in devices:
        d = root / name
        d.mkdir(parents=True)
        for k, v in (("idVendor", f"{vid:04x}"), ("idProduct", f"{pid:04x}")):
            (d / k).write_text(v + "\n")
        (d / "busnum").write_text(f"{busnum}\n")
        (d / "devnum").write_text(f"{devnum}\n")
    (root / "1-0:1.0").mkdir(exist_ok=True)
    return str(root)


XUM = (opencbm.XUM1541_VID, opencbm.XUM1541_PID)


def test_usbdevfs_reset_number_matches_linux():
    """_IO('U', 20): type 'U' in bits 8-15, nr 20, no direction or size."""
    assert opencbm.USBDEVFS_RESET == ord("U") * 256 + 20


def test_usb_nodes_finds_only_the_adapter(tmp_path):
    sysfs = usb_tree(
        tmp_path,
        ("1-1", 0x046D, 0xC52B, 1, 2),
        ("3-2", *XUM, 3, 17),
        ("usb1", 0x1D6B, 0x0002, 1, 1),
    )
    (tmp_path / "4-1").mkdir()
    (tmp_path / "4-1" / "idVendor").write_text("zz\n")
    assert opencbm.usb_nodes(sysfs, "/u") == ["/u/003/017"]
    assert not opencbm.usb_nodes(str(tmp_path / "none"))


def test_usb_reset_ioctls_the_node_and_reopens(tmp_path, monkeypatch):
    sysfs = usb_tree(tmp_path, ("1-4", *XUM, 1, 9))
    calls = []
    monkeypatch.setattr(opencbm.os, "open", lambda p, f: calls.append((p, f)) or 42)
    monkeypatch.setattr(opencbm.os, "close", lambda fd: calls.append(("close", fd)))
    monkeypatch.setattr(
        opencbm.fcntl, "ioctl", lambda fd, req, arg: calls.append((fd, req, arg))
    )
    lib = FakeLib()
    cbm = OpenCBM("xum1541:0", lib=lib)
    cbm._plugin["x"] = None  # pylint: disable=protected-access
    cbm.usb_reset(sysfs, "/u")
    assert calls == [
        ("/u/001/009", opencbm.os.O_WRONLY),
        (42, opencbm.USBDEVFS_RESET, 0),
        ("close", 42),
    ]
    assert lib.calls == ["cbm_driver_open_ex", "cbm_driver_close", "cbm_driver_open_ex"]
    assert cbm.fd is not None and cbm.adapter == "xum1541:0"
    assert not cbm._plugin  # pylint: disable=protected-access


def test_usb_reset_failures(tmp_path, monkeypatch):
    lib = FakeLib()
    cbm = OpenCBM(lib=lib)
    with pytest.raises(OpenCBMError, match="exactly one 16d0:0504 adapter, found 0"):
        cbm.usb_reset(str(tmp_path / "none"))
    assert lib.calls == ["cbm_driver_open_ex"]
    sysfs = usb_tree(tmp_path / "two", ("1-1", *XUM, 1, 3), ("1-2", *XUM, 1, 4))
    with pytest.raises(OpenCBMError, match="found 2"):
        cbm.usb_reset(sysfs)

    def denied(*_):
        raise PermissionError("denied")

    monkeypatch.setattr(opencbm.os, "open", denied)
    sysfs = usb_tree(tmp_path / "one", ("1-1", *XUM, 2, 5))
    with pytest.raises(OpenCBMError, match="USBDEVFS_RESET on /u/002/005: denied"):
        cbm.usb_reset(sysfs, "/u")
    assert lib.calls[-2:] == ["cbm_driver_close", "cbm_driver_open_ex"]
    assert cbm.fd is not None
