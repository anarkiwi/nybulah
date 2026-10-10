"""ctypes binding to libopencbm and the xum1541 plugin's fast-protocol entry points."""

import ctypes
import ctypes.util
import fcntl
import os
import pathlib

IEC_DATA = 0x01
IEC_CLOCK = 0x02
IEC_ATN = 0x04
IEC_RESET = 0x08
IEC_SRQ = 0x10
IO_TIMEOUT_MS = int(os.environ.get("XUM1541_IO_TIMEOUT_MS", "30000"), 0)

_FD = ctypes.c_ssize_t
_BUF = ctypes.c_char_p
_PROTOS = {
    "cbm_driver_open_ex": (ctypes.c_int, [ctypes.POINTER(_FD), ctypes.c_char_p]),
    "cbm_driver_close": (None, [_FD]),
    "cbm_reset": (ctypes.c_int, [_FD]),
    "cbm_adapter_reset": (ctypes.c_int, [_FD, ctypes.c_int]),
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
    "cbm_talk": (ctypes.c_int, [_FD, ctypes.c_ubyte, ctypes.c_ubyte]),
    "cbm_untalk": (ctypes.c_int, [_FD]),
    "cbm_open": (
        ctypes.c_int,
        [_FD, ctypes.c_ubyte, ctypes.c_ubyte, _BUF, ctypes.c_size_t],
    ),
    "cbm_close": (ctypes.c_int, [_FD, ctypes.c_ubyte, ctypes.c_ubyte]),
    "cbm_raw_read": (ctypes.c_int, [_FD, ctypes.c_void_p, ctypes.c_size_t]),
    "cbm_get_eoi": (ctypes.c_int, [_FD]),
    "cbm_iec_poll": (ctypes.c_int, [_FD]),
    "cbm_unlisten": (ctypes.c_int, [_FD]),
    "cbm_iec_get": (ctypes.c_int, [_FD, ctypes.c_int]),
    "cbm_iec_set": (None, [_FD, ctypes.c_int]),
    "cbm_iec_release": (None, [_FD, ctypes.c_int]),
    "cbm_iec_setrelease": (None, [_FD, ctypes.c_int, ctypes.c_int]),
    "cbm_iec_wait": (ctypes.c_int, [_FD, ctypes.c_int, ctypes.c_int]),
    "cbm_get_plugin_function_address": (ctypes.c_void_p, [ctypes.c_char_p]),
}
_OPTIONAL = {"cbm_adapter_reset"}
_XFER = ctypes.CFUNCTYPE(ctypes.c_int, _FD, ctypes.c_void_p, ctypes.c_uint)
_STREAM = "opencbm_plugin_srq2_stream"
_SET_TIMEOUT = ctypes.CFUNCTYPE(ctypes.c_int, _FD, ctypes.c_uint)

XUM1541_VID, XUM1541_PID = 0x16D0, 0x0504
SYSFS_USB = "/sys/bus/usb/devices"
USBFS = "/dev/bus/usb"
_IOC_NRBITS = 8


def _io(kind, nr):
    """Linux _IO(type, nr) (asm-generic/ioctl.h): no direction, no size."""
    return ord(kind) << _IOC_NRBITS | nr


USBDEVFS_RESET = _io("U", 20)


def usb_nodes(sysfs=SYSFS_USB, usbfs=USBFS, vid=XUM1541_VID, pid=XUM1541_PID):
    """usbfs nodes (usbfs/BBB/DDD) of every USB device vid:pid listed in sysfs."""
    nodes = []
    for d in sorted(p.parent for p in pathlib.Path(sysfs).glob("*/idVendor")):
        try:
            attr = {
                k: int((d / k).read_text(), 16 if k.startswith("id") else 10)
                for k in ("idVendor", "idProduct", "busnum", "devnum")
            }
        except (OSError, ValueError):
            continue
        if (attr["idVendor"], attr["idProduct"]) == (vid, pid):
            nodes.append(f"{usbfs}/{attr['busnum']:03d}/{attr['devnum']:03d}")
    return nodes


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
    """Load libopencbm and attach prototypes; an older library may lack the
    _OPTIONAL entry points, whose methods then raise OpenCBMError."""
    lib = ctypes.CDLL(ctypes.util.find_library(name) or f"lib{name}.so.0")
    for fn, (restype, argtypes) in _PROTOS.items():
        f = getattr(lib, fn, None)
        if f is None and fn in _OPTIONAL:
            continue
        if f is None:
            raise OpenCBMError(f"lib{name} lacks {fn}")
        f.restype, f.argtypes = restype, argtypes
    return lib


class OpenCBM:  # pylint: disable=too-many-public-methods
    """An open OpenCBM driver handle (one ZoomFloppy/xum1541)."""

    def __init__(self, adapter=None, lib=None):
        self.lib = lib or load_library()
        self.adapter = adapter
        self._open()

    def _open(self):
        self.fd = _FD()
        self._plugin = {}
        if self.lib.cbm_driver_open_ex(
            ctypes.byref(self.fd), self.adapter.encode() if self.adapter else None
        ):
            raise OpenCBMError("cbm_driver_open_ex failed")

    def usb_reset(self, sysfs=SYSFS_USB, usbfs=USBFS):
        """USB-reset the xum1541 (USBDEVFS_RESET on its usbfs node) and reopen
        the driver: clears an adapter whose firmware no longer runs its command
        loop, which a RESET request cannot reach."""
        nodes = usb_nodes(sysfs, usbfs)
        if len(nodes) != 1:
            raise OpenCBMError(
                f"USB reset needs exactly one {XUM1541_VID:04x}:{XUM1541_PID:04x} "
                f"adapter, found {len(nodes)}"
            )
        self.close()
        try:
            fd = os.open(nodes[0], os.O_WRONLY)
            try:
                fcntl.ioctl(fd, USBDEVFS_RESET, 0)
            finally:
                os.close(fd)
        except OSError as e:
            raise OpenCBMError(f"USBDEVFS_RESET on {nodes[0]}: {e}") from e
        finally:
            self._open()

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

    def adapter_reset(self, reset_bus=True):
        """Reset the adapter from its control endpoint (xum1541 firmware v13),
        aborting any transfer even with its command loop wedged; reset_bus also
        pulses IEC RESET."""
        fn = getattr(self.lib, "cbm_adapter_reset", None)
        if fn is None:
            raise OpenCBMError("libopencbm lacks cbm_adapter_reset")
        self._check(fn(self.fd, int(bool(reset_bus))), "cbm_adapter_reset", 0)

    def unlisten(self):
        """UNLISTEN under ATN to every device (clears a 1571/1581 fast host flag)."""
        self._check(self.lib.cbm_unlisten(self.fd), "cbm_unlisten")

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

    def talk(self, dev, sa):
        """Address dev as talker on secondary address sa."""
        self._check(self.lib.cbm_talk(self.fd, dev, sa), "cbm_talk", 0)

    def untalk(self):
        """Release the talker."""
        self._check(self.lib.cbm_untalk(self.fd), "cbm_untalk", 0)

    def open_file(self, dev, sa, name):
        """OPEN name on dev's secondary address sa (LISTEN, name, UNLISTEN)."""
        name = bytes(name)
        rc = self.lib.cbm_open(self.fd, dev, sa, name, len(name))
        self._check(rc, "cbm_open", 0)

    def close_file(self, dev, sa):
        """CLOSE dev's secondary address sa (LISTEN, CLOSE, UNLISTEN)."""
        self._check(self.lib.cbm_close(self.fd, dev, sa), "cbm_close", 0)

    def raw_read(self, size):
        """Read up to size bytes from the talker; fewer at EOI."""
        buf = ctypes.create_string_buffer(size)
        n = self._check(self.lib.cbm_raw_read(self.fd, buf, size), "cbm_raw_read")
        return buf.raw[:n]

    def get_eoi(self):
        """Whether the talker signalled EOI on the last byte."""
        return bool(self.lib.cbm_get_eoi(self.fd))

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

    def srq2_stream(self, size):
        """Streaming receive from a 1571 at 2 MHz (firmware v12): the adapter's
        output, at most size bytes, ended by its in-band trailer."""
        buf = ctypes.create_string_buffer(size)
        n = self._check(self._xfer(_STREAM)(self.fd, buf, size), "srq2_stream")
        return buf.raw[:n]

    def supports(self, protocol):
        """Whether plugin and firmware speak protocol; s3 (X), xb (burst X) and
        s4/srq (SRQ) are probed with an empty read, "stream" with an empty stream."""
        if protocol == "stream":
            try:
                return self._xfer(_STREAM)(self.fd, None, 0) == 0
            except OpenCBMError:
                return False
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
