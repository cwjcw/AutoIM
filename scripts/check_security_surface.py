from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Split marker fragments so the scanner does not flag its own source file.
_MARKERS = (
    "readprocess" + "memory",
    "writeprocess" + "memory",
    "createremote" + "thread",
    "fri" + "da",
    "min" + "hook",
    "dll " + "injection",
    "mitm",
    "decrypt " + "database",
)

_COLLECTOR_INPUT = frozenset({"mouse_event", "SetCursorPos", "click_screen_point", "SendInput"})
_COLLECTOR_FORBIDDEN = frozenset({
    "keybd_event", "SendInput", "PostMessage", "SendMessage", "send_shortcut", "send_keys", "type_keys",
    "ensure_foreground", "SetForegroundWindow", "activate", "read_clipboard", "read_unicode_text",
    "copy_visible_message", "safe_click",
})


def inspect_collector_input(path: Path, source: str) -> list[str]:
    """Keep native collector input confined to the B2 and profile mouse gateway."""
    tree = ast.parse(source, filename=str(path))
    checks = []
    navigation = path.name == "collection_navigation.py"
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imported = (*[alias.name for alias in node.names], getattr(node, "module", "") or "")
            if any(value.rsplit(".", 1)[-1] in ("keyboard", "mouse", "driver", "pyautogui", "pynput")
                   for value in imported):
                checks.append(f"{node.lineno}: 采集器引入了通用输入能力")
        if not isinstance(node, ast.Call):
            continue
        function = getattr(node.func, "attr", getattr(node.func, "id", ""))
        if function in _COLLECTOR_FORBIDDEN or (function in _COLLECTOR_INPUT and not navigation):
            checks.append(f"{node.lineno}: 采集器包含未授权调用 {function}")
        if navigation and function == "mouse_event":
            if not node.args or getattr(node.args[0], "attr", "") not in (
                    "MOUSEEVENTF_LEFTDOWN", "MOUSEEVENTF_LEFTUP", "MOUSEEVENTF_WHEEL"):
                checks.append(f"{node.lineno}: 采集网关包含未授权的鼠标事件")
    if navigation:
        gateway = next((node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_act"), None)
        guarded = {id(node) for node in ast.walk(gateway)} if gateway else set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and getattr(node.func, "attr", "") in _COLLECTOR_INPUT
                    and id(node) not in guarded):
                checks.append(f"{node.lineno}: 输入调用位于采集网关之外")
        if gateway is None or not {"perform", "ensure_current", "verify_view", "_profile_point", "contains"} <= {
                node.func.attr for node in ast.walk(gateway) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)}:
            checks.append("缺少 B2、资料、窗口或许可复核")
    return [f"{path.name}: {item}" for item in checks]


def inspect_security_surface() -> list[str]:
    checked = [ROOT / "pyproject.toml", *(ROOT / "src" / "autoim").rglob("*.py")]
    findings: list[str] = []
    for path in checked:
        source = path.read_text(encoding="utf-8", errors="replace")
        content = source.casefold()
        for marker in _MARKERS:
            if marker in content:
                findings.append(f"{path.relative_to(ROOT)}: 命中待人工审查标记 {marker!r}")
        if path.name.startswith("collection_") and path.suffix == ".py":
            findings.extend(inspect_collector_input(path, source))
    return findings


def main() -> int:
    findings = inspect_security_surface()
    if findings:
        print("发现需要人工审查的源代码或依赖项；扫描器不会自动删除内容：")
        print("\n".join(findings))
        return 1
    print("安全表层检查通过：源代码与依赖声明未命中预设危险能力标记。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
