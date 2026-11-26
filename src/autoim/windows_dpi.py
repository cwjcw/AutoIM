"""Windows DPI initialization and scoped per-monitor thread awareness."""

from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache, wraps
import ctypes
from ctypes import wintypes
import sys


@dataclass(frozen=True, slots=True)
class DpiStatus:
    process: str
    thread: str
    method: str = "query"
    error: int | None = None


@lru_cache(maxsize=64)
def win_api(dll, name, args, result):
    api = getattr(ctypes.WinDLL(dll, use_last_error=True), name)
    api.argtypes = args
    api.restype = result
    return api


def _user(name, args, result):
    return win_api("user32", name, args, result)


def thread_awareness():
    if sys.platform != "win32":
        return "UNAVAILABLE"
    try:
        context = _user("GetThreadDpiAwarenessContext", (), ctypes.c_void_p)()
        equal = _user("AreDpiAwarenessContextsEqual", (ctypes.c_void_p, ctypes.c_void_p), wintypes.BOOL)
        for value, name in ((-4, "PER_MONITOR_V2"), (-3, "PER_MONITOR_V1"), (-2, "SYSTEM"),
                            (-5, "UNAWARE_GDI_SCALED"), (-1, "UNAWARE")):
            if equal(context, value):
                return name
    except (AttributeError, OSError):
        pass
    return "UNKNOWN"


def dpi_status(method="query", error=None):
    process = "UNAVAILABLE"
    if sys.platform == "win32":
        mode = ctypes.c_int(-1)
        try:
            query = win_api("shcore", "GetProcessDpiAwareness", (wintypes.HANDLE, ctypes.POINTER(ctypes.c_int)),
                            ctypes.c_long)
            process = {0: "UNAWARE", 1: "SYSTEM", 2: "PER_MONITOR"}.get(mode.value, "UNKNOWN") if (
                query(None, ctypes.byref(mode)) == 0) else "UNKNOWN"
        except (OSError, AttributeError):
            process = "UNKNOWN"
    return DpiStatus(process, thread_awareness(), method, error)


def initialize_dpi():
    if sys.platform != "win32":
        return dpi_status()
    before = dpi_status()
    if before.thread.startswith("PER_MONITOR"):
        return dpi_status("already_per_monitor")
    try:
        set_default = _user("SetProcessDpiAwarenessContext", (ctypes.c_void_p,), wintypes.BOOL)
        for level in (-4, -3, -2):
            ctypes.set_last_error(0)
            if set_default(level):
                return dpi_status(f"SetProcessDpiAwarenessContext({level})")
            error = ctypes.get_last_error()
            if error == 5:
                return dpi_status("process_default_already_set", error)
        return dpi_status("SetProcessDpiAwarenessContext", error)
    except (AttributeError, OSError):
        try:
            result = win_api("shcore", "SetProcessDpiAwareness", (ctypes.c_int,), ctypes.c_long)(2)
            return dpi_status("SetProcessDpiAwareness", result or None)
        except (AttributeError, OSError):
            try:
                ok = _user("SetProcessDPIAware", (), wintypes.BOOL)()
                return dpi_status("SetProcessDPIAware", None if ok else ctypes.get_last_error())
            except (AttributeError, OSError):
                return dpi_status("unavailable")


@contextmanager
def physical_thread():
    if sys.platform != "win32":
        yield
        return
    previous = None
    try:
        if not thread_awareness().startswith("PER_MONITOR"):
            try:
                setter = _user("SetThreadDpiAwarenessContext", (ctypes.c_void_p,), ctypes.c_void_p)
                previous = setter(-4) or setter(-3)
            except (AttributeError, OSError):
                pass
        yield
    finally:
        if previous:
            setter(previous)


def physical_coordinates(function):
    @wraps(function)
    def scoped(*args, **kwargs):
        with physical_thread():
            return function(*args, **kwargs)
    return scoped


def window_dpi(hwnd):
    return int(_user("GetDpiForWindow", (wintypes.HWND,), wintypes.UINT)(hwnd))


def window_awareness(hwnd):
    context = _user("GetWindowDpiAwarenessContext", (wintypes.HWND,), ctypes.c_void_p)(hwnd)
    value = _user("GetAwarenessFromDpiAwarenessContext", (ctypes.c_void_p,), ctypes.c_int)(context)
    return {0: "UNAWARE", 1: "SYSTEM", 2: "PER_MONITOR"}.get(value, "UNKNOWN")
