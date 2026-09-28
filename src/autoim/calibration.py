from __future__ import annotations

import json
from pathlib import Path

from autoim.vision import NormalizedRect


class CalibrationStore:
    """Persist only window-relative normalized areas; never screen coordinates."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, NormalizedRect]:
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return {name: NormalizedRect(**values) for name, values in raw.get("regions", {}).items()}

    def save_region(self, name: str, rect: NormalizedRect) -> None:
        regions = self.load()
        regions[name] = rect
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "regions": {key: vars(value) if hasattr(value, "__dict__") else {
            "left": value.left, "top": value.top, "right": value.right, "bottom": value.bottom
        } for key, value in regions.items()}}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def delete_region(self, name: str) -> None:
        regions = self.load()
        regions.pop(name, None)
        if regions:
            self.path.write_text(json.dumps({"version": 1, "regions": {
                key: {"left": value.left, "top": value.top, "right": value.right, "bottom": value.bottom}
                for key, value in regions.items()
            }}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        elif self.path.exists():
            self.path.unlink()
