"""Read only, HWND scoped image capture for PC-V1."""

from __future__ import annotations

import ctypes
import math
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter

import numpy as np

from autoim.vision import WindowCapture
from autoim.windows_dpi import physical_coordinates
from autoim.wecom.window_coordinates import CoordinateError, WindowCoordinates, build_coordinates, cursor_position
from autoim.wecom.window_manager import WeComWindowInfo, WeComWindowManager, WindowResolutionError
from autoim.wecom.window_state import WindowHealth, probe_window, window_diagnostic


class CaptureError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code} · {detail}")
        self.code = code
        self.diagnostics: CaptureDiagnostics | None = None
        self.frame: VisualFrame | None = None
        self.image: np.ndarray | None = None
        self.probe_size: tuple[int, int] | None = None

    def __str__(self) -> str:
        message = super().__str__()
        d = self.diagnostics
        if d is None:
            return message
        hwnd = f"0x{d.hwnd:X}" if d.hwnd else "未知"
        summary = (f"{message}\nHWND {hwnd} · 物理窗口 {d.window_rect or '未知'} · "
                f"客户区 {d.client_size or '未知'} · DPI {d.dpi or '未知'} · "
                f"原图 {d.raw_size or '无'} · 采集 {d.elapsed_ms:.0f} ms · 识别未执行")
        return summary + (f"\n{window_diagnostic(d.window_health)}" if d.window_health else "")


@dataclass(frozen=True, slots=True)
class CaptureDiagnostics:
    hwnd: int | None = None
    window_rect: tuple[int, int, int, int] | None = None
    client_size: tuple[int, int] | None = None
    dpi: int | None = None
    raw_size: tuple[int, int] | None = None
    elapsed_ms: float = 0
    coverage: str = ""
    probe_size: tuple[int, int] | None = None
    window_health: WindowHealth | None = None


@dataclass(frozen=True, slots=True)
class WindowState:
    minimized: bool
    maximized: bool
    dpi: int
    width: int
    height: int
    mouse: tuple[int, int] | None


@dataclass(frozen=True, slots=True)
class VisualFrame:
    capture: WindowCapture
    captured_at: datetime
    state: WindowState
    mouse: tuple[int, int] | None = None
    diagnostics: CaptureDiagnostics = CaptureDiagnostics()
    coordinates: WindowCoordinates | None = None

    @property
    def pixels(self) -> np.ndarray:
        return self.capture.image

    @property
    def visual_unit(self):
        return self.coordinates.visual_unit if self.coordinates else max(.65, min(self.state.dpi / 96, 3.0))


def _client_mouse(hwnd: int) -> tuple[int, int] | None:
    import win32gui

    try:
        position = win32gui.GetCursorPos()
        pointed = win32gui.WindowFromPoint(position)
        if pointed and win32gui.GetAncestor(pointed, 2) == hwnd:
            return win32gui.ScreenToClient(hwnd, position)
    except win32gui.error:
        # An inaccessible input desktop only removes optional hover evidence.
        pass
    return None


def _capture_state(health: WindowHealth) -> WindowState:
    sample = health.last
    width, height = sample.client_size
    return WindowState(False, bool(sample.zoomed), sample.dpi, width, height, _client_mouse(health.window.hwnd))


class _BitmapHeader(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD), ("width", wintypes.LONG), ("height", wintypes.LONG),
        ("planes", wintypes.WORD), ("bits", wintypes.WORD), ("compression", wintypes.DWORD),
        ("image_size", wintypes.DWORD), ("x_ppm", wintypes.LONG), ("y_ppm", wintypes.LONG),
        ("colors", wintypes.DWORD), ("important", wintypes.DWORD),
    ]


_UNPAINTED = (29, 251, 13)
_MAX_CAPTURE_PIXELS = 20_000_000


def _render_client(hwnd: int, width: int, height: int, *, flags=1) -> np.ndarray:
    print_window = ctypes.windll.user32.PrintWindow
    print_window.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    return _read_bitmap(width, height, lambda hdc: print_window(hwnd, hdc, flags))


def _read_bitmap(width, height, draw):
    gdi = ctypes.windll.gdi32
    create_dib = gdi.CreateDIBSection
    create_dib.argtypes = [wintypes.HDC, ctypes.POINTER(_BitmapHeader), wintypes.UINT,
                           ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    create_dib.restype = wintypes.HBITMAP
    gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
    gdi.SelectObject.restype = wintypes.HANDLE
    gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi.CreateCompatibleDC.restype = wintypes.HDC
    gdi.DeleteDC.argtypes = [wintypes.HDC]
    gdi.DeleteObject.argtypes = [wintypes.HANDLE]
    header = _BitmapHeader(ctypes.sizeof(_BitmapHeader), width, -height, 1, 32, 0)
    bits = ctypes.c_void_p()
    hdc = gdi.CreateCompatibleDC(None)
    if not hdc:
        raise CaptureError("CAPTURE_FAILED", "Windows 未能创建兼容绘图上下文。")
    bitmap = previous = None
    try:
        bitmap = create_dib(hdc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
        if not bitmap or not bits.value:
            raise CaptureError("CAPTURE_FAILED", "无法创建目标窗口图像缓冲区。")
        previous = gdi.SelectObject(hdc, bitmap)
        if not previous or previous == ctypes.c_void_p(-1).value:
            previous = None
            raise CaptureError("CAPTURE_FAILED", "无法选中目标窗口图像缓冲区。")
        buffer = np.ctypeslib.as_array((ctypes.c_uint32 * (width * height)).from_address(bits.value))
        b, g, r = _UNPAINTED
        buffer.fill(b | g << 8 | r << 16)
        if not draw(hdc):
            raise CaptureError("CAPTURE_FAILED", "企业微信未返回客户区图像。")
        gdi.GdiFlush()
        data = ctypes.string_at(bits, width * height * 4)
        return np.frombuffer(data, dtype=np.uint8).reshape(height, width, 4)[:, :, :3].copy()
    finally:
        if previous:
            gdi.SelectObject(hdc, previous)
        if bitmap:
            gdi.DeleteObject(bitmap)
        gdi.DeleteDC(hdc)


@physical_coordinates
def capture_screen_region(region):
    """Read the caller-verified local physical crop for a transient tooltip."""
    from autoim.windows_dpi import win_api

    left, top, right, bottom = region
    width, height = right - left, bottom - top
    if not 1 <= width <= 720 or not 1 <= height <= 450:
        raise CaptureError("HOVER_CROP_INVALID", "悬停截图范围无效")
    source = win_api("user32", "GetDC", (wintypes.HWND,), wintypes.HDC)(None)
    if not source:
        raise CaptureError("HOVER_CROP_UNAVAILABLE", "无法读取局部可见屏幕")
    try:
        draw = win_api("gdi32", "BitBlt", (wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                       ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD), wintypes.BOOL)
        return _read_bitmap(width, height, lambda target: draw(target, 0, 0, width, height, source, left, top, 0x00CC0020))
    finally:
        win_api("user32", "ReleaseDC", (wintypes.HWND, wintypes.HDC), ctypes.c_int)(None, source)


def _identity(info: WeComWindowInfo) -> tuple:
    return info.pid, info.hwnd, info.rect, info.title, info.class_name


@dataclass(frozen=True, slots=True)
class RenderedClient:
    pixels: np.ndarray
    probe_size: tuple[int, int]


def _render_complete_client(hwnd: int, width: int, height: int, *, render=_render_client) -> RenderedClient:
    """Find the client paint extent in a sentinel-filled DIB with a guard band."""
    limit = math.sqrt(_MAX_CAPTURE_PIXELS / (width * height))
    previous_size = None
    last_pixels = None
    for scale in (1.20, 1.56, 2.03, 2.64, 3.44):
        scale = min(scale, limit)
        probe_width, probe_height = int(width * scale), int(height * scale)
        size = probe_width, probe_height
        if size == previous_size or size[0] * size[1] > _MAX_CAPTURE_PIXELS:
            break
        previous_size = size
        pixels = last_pixels = render(hwnd, *size)
        painted = np.any(pixels != _UNPAINTED, axis=2)
        rows = np.flatnonzero(painted.any(axis=1))
        columns = np.flatnonzero(painted.any(axis=0))
        if not rows.size or not columns.size:
            raise CaptureError("CAPTURE_FAILED", "PrintWindow 未绘制客户区像素。")
        actual_width, actual_height = int(columns[-1]) + 1, int(rows[-1]) + 1
        if probe_width - actual_width < 6 or probe_height - actual_height < 6:
            continue
        scale_x, scale_y = actual_width / width, actual_height / height
        if (rows[0] != 0 or columns[0] != 0 or min(scale_x, scale_y) < .98
                or abs(scale_x - scale_y) > .025 * max(scale_x, scale_y)):
            error = CaptureError("CAPTURE_INCOMPLETE",
                                 f"缓冲区 {size} 仅绘制 {actual_width}×{actual_height}，与客户区 {width}×{height} 不一致。")
            error.image, error.probe_size = pixels[:actual_height, :actual_width].copy(), size
            raise error
        return RenderedClient(pixels[:actual_height, :actual_width].copy(), size)
    error = CaptureError("CAPTURE_INCOMPLETE",
                         f"探测缓冲区 {previous_size} 的右或下边缘仍被绘制，未确认画面边界。")
    error.image, error.probe_size = last_pixels, previous_size
    raise error


def _coverage(pixels: np.ndarray) -> tuple[str, str]:
    # A correctly sized DIB can still contain untouched rows or columns.
    # Contiguous black padding is separate evidence, since Windows may fill a
    # larger area than the client paints in that call.
    sample = pixels[::2, ::2]
    sentinel = np.all(sample == _UNPAINTED, axis=2)
    black = np.max(sample, axis=2) <= 3
    height, width = sample.shape[:2]
    right = black[:, -max(3, width // 25):].mean()
    bottom = black[-max(3, height // 25):].mean()
    left = black[:, :max(3, width // 25)].mean()
    top = black[:max(3, height // 25), :].mean()
    core = black[height//8:max(1, height * 7 // 8), width//8:max(1, width * 7 // 8)].mean()
    untouched = sentinel.mean()
    summary = f"未绘制像素 {untouched:.2%} · 右缘近黑 {right:.2%} · 底缘近黑 {bottom:.2%}"
    if (untouched >= .01
            or np.any(sentinel[:, -max(3, width // 50):].mean(axis=0) > .98)
            or np.any(sentinel[-max(3, height // 50):].mean(axis=1) > .98)):
        return "CAPTURE_INCOMPLETE", summary
    if not np.any(np.ptp(sample[::4, ::4], axis=(0, 1)) > 5):
        return "CAPTURE_FAILED", f"截图缺少可用图像变化 · {summary}"
    if max(left, top, right, bottom) > .98 and core < .50:
        return "CAPTURE_INCOMPLETE", f"窗口图像存在宽黑边 · {summary}"
    return "", summary


@physical_coordinates
def capture_visible_window(manager: WeComWindowManager | None = None, *,
                           inspect=None, render=_render_complete_client, probe=probe_window) -> VisualFrame:
    started = perf_counter()
    manager = manager or WeComWindowManager()
    target = state = pixels = probe_size = health = captured_at = None

    def details(coverage: str = "") -> CaptureDiagnostics:
        sample = health.last if health else None
        return CaptureDiagnostics(
            health.window.hwnd if health and health.window else target.hwnd if target else None,
            sample.rect if sample else target.rect if target else None,
            sample.client_size if sample else (state.width, state.height) if state else None,
            sample.dpi if sample else state.dpi if state else None,
            (pixels.shape[1], pixels.shape[0]) if isinstance(pixels, np.ndarray) and pixels.ndim >= 2 else None,
            (perf_counter() - started) * 1000, coverage, probe_size, health,
        )

    def failure(code: str, message: str, coverage: str = "") -> CaptureError:
        error = CaptureError(code, message)
        error.diagnostics = details(coverage)
        if state and target and isinstance(pixels, np.ndarray) and pixels.ndim == 3:
            h, w = pixels.shape[:2]
            error.frame = VisualFrame(WindowCapture(target.pid, target.hwnd, target.title, target.class_name,
                                                   target.rect, w, h, pixels, state.dpi / 96),
                                      captured_at or datetime.now().astimezone(), state, diagnostics=error.diagnostics)
        return error

    if inspect is None:
        health = probe(manager)
        blocker = health.capture_blocker
        if blocker:
            reason = "请手动最大化企业微信。" if blocker == "NOT_MAXIMIZED" else health.reason
            raise failure(blocker, reason)
        target = health.window
        state = _capture_state(health)
    else:
        # Keep the injectable capture boundary for synthetic HWND/render tests.
        try:
            target = manager.resolve_window()
            state = inspect(target.hwnd)
        except (WindowResolutionError, OSError) as exc:
            raise failure("WINDOW_UNAVAILABLE", "找不到可验证的企业微信窗口。") from exc
        except Exception as exc:
            raise failure("WINDOW_CHANGED", "读取窗口状态时窗口已失效。") from exc
    if state.minimized:
        raise failure("WINDOW_MINIMIZED", "请手动恢复并最大化企业微信。")
    if not state.maximized and (health is None or health.capture_blocker):
        raise failure("NOT_MAXIMIZED", "窗口过小或无法核对客户区。")
    if target.rect is None or state.dpi < 1 or min(state.width, state.height) < 160:
        raise failure("WINDOW_CHANGED", "窗口矩形、客户区或 DPI 无效。")
    if state.width * state.height > _MAX_CAPTURE_PIXELS:
        raise failure("CAPTURE_FAILED", "客户区超过 2000 万像素。")
    before = health
    render_error = None
    try:
        rendered = render(target.hwnd, state.width, state.height)
        if isinstance(rendered, RenderedClient):
            pixels, probe_size = rendered.pixels, rendered.probe_size
        else:
            pixels = rendered
        captured_at = datetime.now().astimezone()
    except Exception as exc:
        render_error = exc
    try:
        if inspect is None:
            health = probe(manager)
            if health.capture_blocker or health.identity != before.identity or health.geometry_key != before.geometry_key:
                raise ValueError("window state or process changed")
            current = health.window
            current_state = _capture_state(health)
        else:
            current = manager.resolve_window()
            current_state = inspect(current.hwnd)
        if (_identity(current) != _identity(target)
                or (current_state.width, current_state.height, current_state.dpi)
                != (state.width, state.height, state.dpi)
                or current_state.minimized or current_state.maximized != state.maximized):
            raise ValueError("window changed")
    except Exception as exc:
        raise failure("WINDOW_CHANGED", "截图期间窗口身份、大小、位置、DPI 或状态发生变化。") from exc
    if isinstance(render_error, CaptureError):
        pixels, probe_size = render_error.image, render_error.probe_size
        raise failure(render_error.code, str(render_error)) from render_error
    if render_error is not None:
        raise failure("CAPTURE_FAILED", "Windows 未能渲染企业微信客户区。") from render_error
    if (not isinstance(pixels, np.ndarray) or pixels.ndim != 3 or pixels.shape[2] != 3
            or pixels.dtype != np.uint8
            or (probe_size is None and pixels.shape[:2] != (state.height, state.width))):
        raise failure("CAPTURE_FAILED", "原始图像尺寸或像素格式无效。")
    code, coverage = _coverage(pixels)
    if code:
        raise failure(code, coverage, coverage)
    pixels.setflags(write=False)
    height, width = pixels.shape[:2]
    coordinates = None
    mouse = state.mouse if current_state.mouse == state.mouse else None
    if inspect is None and before.last.desktop is not None:
        try:
            coordinates = build_coordinates(target.pid, target.hwnd, target.class_name, (state.width, state.height),
                                            (width, height), state.dpi, before.last.desktop, paint_verified=True)
        except CoordinateError as error:
            raise failure("COORDINATE_MAPPING_UNVERIFIED", str(error), coverage) from error
    elif inspect is None and render is _render_complete_client:
        raise failure("COORDINATE_MAPPING_UNVERIFIED", "未取得可独立复核的物理桌面与客户区映射", coverage)
    if mouse is not None:
        if coordinates:
            try:
                mouse = coordinates.screen_to_image(cursor_position())
            except (CoordinateError, OSError):
                mouse = None
        else:
            mouse = (round(mouse[0] * width / state.width), round(mouse[1] * height / state.height))
    if probe_size:
        coverage = f"缓冲区 {probe_size[0]}×{probe_size[1]} 的右、底留有未绘制边带 · {coverage}"
    return VisualFrame(WindowCapture(target.pid, target.hwnd, target.title, target.class_name,
                                     target.rect, width, height, pixels, state.dpi / 96),
                       captured_at, state, mouse, details(coverage), coordinates)
