"""Evidence-backed transforms between original image, Win32 client and physical desktop."""

from dataclasses import asdict, dataclass
from hashlib import blake2s
from math import floor
from ctypes import wintypes
import ctypes
import json

from autoim.windows_dpi import DpiStatus, dpi_status, win_api, window_awareness

Point = tuple[int, int]
Rect = tuple[int, int, int, int]


class CoordinateError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DesktopGeometry:
    client_origin: Point
    client_end: Point
    frame_physical: Rect
    monitor: str
    monitor_rect: Rect
    native_monitor_rect: Rect
    virtual_desktop: Rect
    roundtrip_error: int
    awareness: DpiStatus
    window_awareness: str
    monitors: int = 1
    remote: bool = False

    @property
    def identity(self):
        return (self.client_origin, self.client_end, self.frame_physical, self.monitor,
                self.monitor_rect, self.native_monitor_rect, self.virtual_desktop,
                self.monitors, self.remote)


def _user(name, args, result):
    return win_api("user32", name, args, result)


def probe_desktop(hwnd, client_size, gui):
    import win32api

    status = dpi_status()
    width, height = client_size
    if min(width, height) < 1:
        return None
    points = ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1), (width // 2, height // 2))
    screen = tuple(gui.ClientToScreen(hwnd, point) for point in points)
    origin = screen[0]
    expected = tuple((origin[0] + x, origin[1] + y) for x, y in points)
    back = tuple(gui.ScreenToClient(hwnd, point) for point in screen)
    error = max(abs(value - wanted) for actual, reference in ((screen, expected), (back, points))
                for actual_point, reference_point in zip(actual, reference)
                for value, wanted in zip(actual_point, reference_point))
    bounds = wintypes.RECT()
    dwm = win_api("dwmapi", "DwmGetWindowAttribute",
                  (wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD), ctypes.c_long)
    if dwm(hwnd, 9, ctypes.byref(bounds), ctypes.sizeof(bounds)) != 0:
        return None
    monitor = win32api.GetMonitorInfo(win32api.MonitorFromWindow(hwnd, 2))
    mode = win32api.EnumDisplaySettings(monitor["Device"], -1)
    x, y = mode.Position_x, mode.Position_y
    physical = (x, y, x + mode.PelsWidth, y + mode.PelsHeight)
    # EnumDisplaySettings reports the device pixels independently of the caller's DPI virtualization.
    metric = lambda index: int(win32api.GetSystemMetrics(index))
    vx, vy = metric(76), metric(77)
    return DesktopGeometry(origin, (screen[3][0] + 1, screen[3][1] + 1),
                           (bounds.left, bounds.top, bounds.right, bounds.bottom), monitor["Device"],
                           tuple(monitor["Monitor"]), physical, (vx, vy, vx + metric(78), vy + metric(79)),
                           error, status, window_awareness(hwnd), metric(80), bool(metric(0x1000)))


def inside(rect, point):
    left, top, right, bottom = rect
    return left <= point[0] < right and top <= point[1] < bottom


@dataclass(frozen=True, slots=True)
class WindowCoordinates:
    pid: int
    hwnd: int
    class_name: str
    client_size: Point
    image_size: Point
    dpi: int
    desktop: DesktopGeometry
    method: str
    image_origin: Point = (0, 0)

    @property
    def render_scale(self):
        return tuple(image / client for image, client in zip(self.image_size, self.client_size))

    @property
    def snapshot(self):
        key = (self.pid, self.hwnd, self.class_name, self.client_size, self.image_size, self.dpi,
               self.desktop.identity, self.method, self.image_origin)
        return blake2s(repr(key).encode(), digest_size=10).hexdigest()

    @property
    def visual_unit(self):
        # Existing templates are calibrated on 96-DPI original raster pixels.
        # The rendered bitmap already incorporates the client scaling.
        return max(.65, min(self.dpi / 96, 3.0))

    def image_to_client(self, point):
        if not inside((*self.image_origin, *self.image_size), point):
            raise CoordinateError("POINT_OUTSIDE_IMAGE")
        return tuple(min(client - 1, floor((value + .5) * client / image))
                     for value, image, client in zip(point, self.image_size, self.client_size))

    def client_to_image(self, point):
        if not inside((0, 0, *self.client_size), point):
            raise CoordinateError("POINT_OUTSIDE_CLIENT")
        return tuple(min(image - 1, floor((value + .5) * image / client))
                     for value, image, client in zip(point, self.image_size, self.client_size))

    def client_to_screen(self, point):
        if not inside((0, 0, *self.client_size), point):
            raise CoordinateError("POINT_OUTSIDE_CLIENT")
        return tuple(origin + value for origin, value in zip(self.desktop.client_origin, point))

    def screen_to_client(self, point):
        client = tuple(value - origin for origin, value in zip(self.desktop.client_origin, point))
        if not inside((0, 0, *self.client_size), client):
            raise CoordinateError("POINT_OUTSIDE_CLIENT")
        return client

    def image_to_screen(self, point, *, for_input=False):
        target = self.client_to_screen(self.image_to_client(point))
        if for_input and not inside(self.desktop.monitor_rect, target):
            raise CoordinateError("POINT_OUTSIDE_VERIFIED_MONITOR")
        return target

    def screen_to_image(self, point):
        return self.client_to_image(self.screen_to_client(point))

    def image_rect_to_screen(self, region):
        left, top = self.image_to_screen(region[:2])
        right, bottom = self.image_to_screen((region[2] - 1, region[3] - 1))
        return left, top, right + 1, bottom + 1

    def diagnostic(self):
        return {**asdict(self), "snapshot": self.snapshot, "render_scale": self.render_scale,
                "status": "VERIFIED", "spaces": ("IMAGE_PIXEL", "CLIENT_COORDINATE", "SCREEN_PHYSICAL")}


def build_coordinates(pid, hwnd, class_name, client_size, image_size, dpi, desktop, *, paint_verified,
                      image_origin=(0, 0)):
    if not paint_verified or image_origin != (0, 0):
        raise CoordinateError("IMAGE_PAINT_OR_ORIGIN_UNVERIFIED")
    if not desktop or not desktop.awareness.thread.startswith("PER_MONITOR"):
        raise CoordinateError("PHYSICAL_THREAD_OR_DESKTOP_UNVERIFIED")
    if not pid or not hwnd or not class_name or not dpi or min(*client_size, *image_size) <= 0:
        raise CoordinateError("WINDOW_GEOMETRY_INVALID")
    if desktop.roundtrip_error > 1 or desktop.monitor_rect != desktop.native_monitor_rect:
        raise CoordinateError("DESKTOP_ROUNDTRIP_OR_MONITOR_UNVERIFIED")
    if tuple(b - a for a, b in zip(desktop.client_origin, desktop.client_end)) != client_size:
        raise CoordinateError("CLIENT_EXTENT_UNVERIFIED")
    tx, ty, bx, by = desktop.frame_physical
    ox, oy = desktop.client_origin
    ex, ey = desktop.client_end
    if not (tx - 16 <= ox < ex <= bx + 16 and ty - 16 <= oy < ey <= by + 16
            and (bx - tx) - client_size[0] <= 96 and (by - ty) - client_size[1] <= 128):
        raise CoordinateError("DWM_FRAME_OR_CLIENT_UNVERIFIED")
    mon = desktop.monitor_rect
    center = ((ox + ex) // 2, (oy + ey) // 2)
    if not inside(mon, center) or not inside(desktop.virtual_desktop, center):
        raise CoordinateError("WINDOW_NOT_ON_VERIFIED_DESKTOP")
    if desktop.monitors > 1 and any((ox < mon[0] - 16, oy < mon[1] - 16, ex > mon[2] + 16, ey > mon[3] + 16)):
        raise CoordinateError("WINDOW_SPANS_UNVERIFIED_MONITORS")
    factors = tuple(image / client for image, client in zip(image_size, client_size))
    identity = all(abs(a-b) <= 1 for a,b in zip(client_size, image_size))
    dpi_scale = dpi / 96
    if not identity and (abs(factors[0] - factors[1]) > .012
                         or any(abs(image - client * dpi_scale) > 2
                                for image, client in zip(image_size, client_size))
                         or desktop.window_awareness == "UNKNOWN"):
        raise CoordinateError("RENDER_SCALE_UNEXPLAINED")
    return WindowCoordinates(pid, hwnd, class_name, client_size, image_size, dpi, desktop,
                             "NATIVE_CLIENT" if identity else "VERIFIED_DPI_RENDER", image_origin)


def cursor_position():
    point = wintypes.POINT()
    if not _user("GetPhysicalCursorPos", (ctypes.POINTER(wintypes.POINT),), wintypes.BOOL)(ctypes.byref(point)):
        raise CoordinateError("PHYSICAL_CURSOR_UNAVAILABLE")
    return point.x, point.y


def move_cursor(point):
    if not _user("SetPhysicalCursorPos", (ctypes.c_int, ctypes.c_int), wintypes.BOOL)(*point):
        raise CoordinateError("PHYSICAL_CURSOR_MOVE_FAILED")


def window_at(point):
    return _user("WindowFromPhysicalPoint", (wintypes.POINT,), wintypes.HWND)(wintypes.POINT(*point))
