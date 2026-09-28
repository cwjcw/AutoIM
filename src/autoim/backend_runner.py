from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

from autoim.diagnostics import scan_backend


def main() -> int:
    if len(sys.argv) != 6:
        return 2
    key, hwnd_text, depth_text, limit_text, output_text = sys.argv[1:]
    try:
        records = scan_backend(key, int(hwnd_text), int(depth_text), int(limit_text))
        payload = {"success": True, "records": records}
        exit_code = 0
    except BaseException as exc:
        payload = {"success": False, "error": f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-3000:]}"}
        exit_code = 1
    Path(output_text).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
