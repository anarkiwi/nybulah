"""ctypes binding to libopencbm and the xum1541 plugin's fast-protocol entry points."""

import ctypes
import ctypes.util

IEC_DATA = 0x01
IEC_CLOCK = 0x02
IEC_ATN = 0x04
IEC_RESET = 0x08
IEC_SRQ = 0x10

_FD = ctypes.c_ssize_t
_BUF = ctypes.c_char_p
_PROTOS = {
    "cbm_driver_open_ex": (ctypes.c_int, [ctypes.POINTER(_FD), ctypes.c_char_p]),
    "cbm_driver_close": (None, [_FD]),
    "cbm_reset": (ctypes.c_int, [_FD]),
    "cbm_upload": (
        ctypes.c_int,
        [_FD, ctypes.c_ubyte, ctypes.c_int, _BUF, ctypes.c_size_t],
    ),
    "cbm_download": (
        ctypes.c_int,
        [_FD, ctypes.c_ubyte, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t],
    ),
    "cbm_exec_command": (ctypes.c_int, [_FD, ctypes.c_ubyte, _BUF, ctypes.c_size_t]),
    "cbm_device_status": (
        ctypes.c_int,
        [_FD, ctypes.c_ubyte, ctypes.c_void_p, ctypes.c_size_t],
    ),
    "cbm_identify": (
        ctypes.c_int,
        [
            _FD,
            ctypes.c_ubyte,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_char_p),
        ],
    ),
    "cbm_iec_poll": (ctypes.c_int, [_FD]),
    "cbm_iec_get": (ctypes.c_int, [_FD, ctypes.c_int]),
    "cbm_iec_set": (None, [_FD, ctypes.c_int]),
    "cbm_iec_release": (None, [_FD, ctypes.c_int]),
    "cbm_iec_setrelease": (None, [_FD, ctypes.c_int, ctypes.c_int]),
    "cbm_iec_wait": (ctypes.c_int, [_FD, ctypes.c_int, ctypes.c_int]),
    "cbm_get_plugin_function_address": (ctypes.c_void_p, [ctypes.c_char_p]),
}
_XFER = ctypes.CFUNCTYPE(ctypes.c_int, _FD, ctypes.c_void_p, ctypes.c_uint)
_SET_TIMEOUT = ctypes.CFUNCTYPE(ctypes.c_int, _FD, ctypes.c_uint)


class OpenCBMError(IOError):
    """A libopencbm call reported failure."""


def _transfers(proto, what):
    """(read, write) methods for the plugin's opencbm_plugin_<proto>_read_n/_write_n."""

    def read(self, size):
        return self.read_n(proto, size)

    def write(self, data):
        self.write_n(proto, data)

    read.__doc__ = f"Read size bytes with {what}."
    write.__doc__ = f"Write bytes with {what}."
    return read, write


def load_library(name="opencbm"):
    """Load libopencbm and attach prototypes."""
    lib = ctypes.CDLL(ctypes.util.find_library(name) or f"lib{name}.so.0")
    for fn, (restype, argtypes) in _PROTOS.items():
        f = getattr(lib, fn)
        f.restype, f.argtypes = restype, argtypes
    return lib


class OpenCBM:
    """An open OpenCBM driver handle (one ZoomFloppy/xum1541)."""

    def __init__(self, adapter=None, lib=None):
        self.lib = lib or load_library()
        self.fd = _FD()
        if self.lib.cbm_driver_open_ex(
            ctypes.byref(self.fd), adapter.encode() if adapter else None
        ):
            raise OpenCBMError("cbm_driver_open_ex failed")
        self._plugin = {}

    def close(self):
        """Release the driver handle."""
        if self.fd is not None:
            self.lib.cbm_driver_close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _check(self, rc, what, expect=None):
        if rc < 0 or (expect is not None and rc != expect):
            raise OpenCBMError(f"{what} returned {rc}")
        return rc

    def reset(self):
        """Pulse IEC RESET."""
        self._check(self.lib.cbm_reset(self.fd), "cbm_reset")

    def identify(self, dev):
        """Return (device type code, description) for a drive."""
        dt, desc = ctypes.c_int(), ctypes.c_char_p()
        self._check(
            self.lib.cbm_identify(self.fd, dev, ctypes.byref(dt), ctypes.byref(desc)),
            "cbm_identify",
        )
        return dt.value, (desc.value or b"").decode(errors="replace")

    def upload(self, dev, addr, data):
        """Write drive memory via M-W."""
        data = bytes(data)
        self._check(
            self.lib.cbm_upload(self.fd, dev, addr, data, len(data)),
            "cbm_upload",
            len(data),
        )

    def download(self, dev, addr, size):
        """Read drive memory via M-R."""
        buf = ctypes.create_string_buffer(size)
        self._check(
            self.lib.cbm_download(self.fd, dev, addr, buf, size), "cbm_download", size
        )
        return buf.raw

    def command(self, dev, cmd):
        """Send a command string to the drive's command channel."""
        cmd = bytes(cmd)
        self._check(
            self.lib.cbm_exec_command(self.fd, dev, cmd, len(cmd)), "cbm_exec_command"
        )

    def status(self, dev):
        """Read the drive's error channel."""
        buf = ctypes.create_string_buffer(64)
        self.lib.cbm_device_status(self.fd, dev, buf, len(buf))
        return buf.value.decode(errors="replace").strip()

    def iec_poll(self):
        """Return the current IEC line state bitmask."""
        return self.lib.cbm_iec_poll(self.fd)

    def iec_set(self, lines):
        """Assert IEC lines."""
        self.lib.cbm_iec_set(self.fd, lines)

    def iec_release(self, lines):
        """Release IEC lines."""
        self.lib.cbm_iec_release(self.fd, lines)

    def iec_wait(self, line, state):
        """Block until line reaches state (asserted=1); returns the line mask."""
        return self.lib.cbm_iec_wait(self.fd, line, state)

    def _xfer(self, name, proto=_XFER):
        if name not in self._plugin:
            addr = self.lib.cbm_get_plugin_function_address(name.encode())
            if not addr:
                raise OpenCBMError(f"plugin lacks {name}")
            self._plugin[name] = proto(addr)
        return self._plugin[name]

    def read_n(self, proto, size):
        """Read size bytes through opencbm_plugin_<proto>_read_n."""
        buf = ctypes.create_string_buffer(size)
        fn = self._xfer(f"opencbm_plugin_{proto}_read_n")
        self._check(fn(self.fd, buf, size), f"{proto}_read", size)
        return buf.raw

    def write_n(self, proto, data):
        """Write bytes through opencbm_plugin_<proto>_write_n."""
        data = bytes(data)
        fn = self._xfer(f"opencbm_plugin_{proto}_write_n")
        self._check(fn(self.fd, data, len(data)), f"{proto}_write", len(data))

    s1_read, s1_write = _transfers("s1", "the S1 protocol (CLK/DATA only)")
    s2_read, s2_write = _transfers("s2", "the S2 protocol (ATN strobed)")
    s3_read, s3_write = _transfers("x", "the X protocol (xum1541 firmware v9+)")
    x2_read, x2_write = _transfers("x2", "X timed for a 1571 at 2 MHz")
    xb_read, xb_write = _transfers("xb", "burst X (xum1541 firmware v10+)")
    xb2_read, xb2_write = _transfers("xb2", "burst X timed for a 1571 at 2 MHz")
    srq_read, srq_write = _transfers("srq", "SRQ fast serial (1571, firmware v11+)")
    srq2_read, srq2_write = _transfers("srq2", "SRQ fast serial, 1571 at 2 MHz")
    s4_read, s4_write = srq_read, srq_write

    PROBES = {"s3": "x", "xb": "xb", "s4": "srq", "srq": "srq"}

    def supports(self, protocol):
        """Whether plugin and firmware speak protocol; s3 (X), xb (burst X) and
        s4/srq (SRQ) are probed with an empty read."""
        if protocol not in self.PROBES:
            return hasattr(self, f"{protocol}_read")
        try:
            self.read_n(self.PROBES[protocol], 0)
        except OpenCBMError:
            return False
        return True

    def set_timeout(self, ms):
        """Set the adapter's I/O idle timeout where the plugin supports it."""
        try:
            fn = self._xfer("opencbm_plugin_xum1541_set_timeout", _SET_TIMEOUT)
        except OpenCBMError:
            return False
        return fn(self.fd, ms) == 0
