from __future__ import annotations

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


def inspect_security_surface() -> list[str]:
    checked = [ROOT / "pyproject.toml", *(ROOT / "src" / "autoim").rglob("*.py")]
    findings: list[str] = []
    for path in checked:
        content = path.read_text(encoding="utf-8", errors="replace").casefold()
        for marker in _MARKERS:
            if marker in content:
                findings.append(f"{path.relative_to(ROOT)}: 命中待人工审查标记 {marker!r}")
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
