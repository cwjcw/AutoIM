"""从独立子进程执行一次后端诊断，并通过 JSON 文件返回结果。"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

from autoim.diagnostics import scan_backend

def main() -> int:
    """解析命令行参数，执行后端扫描，并将成功或失败结果写入交换文件。

    命令行参数依次为后端名称、窗口句柄、扫描深度、控件数量上限和结果文件路径。

    Returns:
        int: 参数无效时返回 2，扫描成功时返回 0，扫描异常时返回 1。
    """
    if len(sys.argv) != 6:
        # 参数数量不符合约定时直接返回参数错误，避免产生未定义的扫描结果。
        return 2
    key, hwnd_text, depth_text, limit_text, output_text = sys.argv[1:]
    try:
        # 将字符串参数转换为整数并调用指定后端进行扫描。
        records = scan_backend(key, int(hwnd_text), int(depth_text), int(limit_text))
        payload = {"success": True, "records": records}
        exit_code = 0
    except BaseException as exc:
        # 捕获子进程中的所有异常，并保留类型、消息及截取后的异常堆栈。
        payload = {"success": False, "error": f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-3000:]}"}
        exit_code = 1
    # ensure_ascii=False 确保中文内容以可读形式写入 JSON 文件。
    Path(output_text).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
