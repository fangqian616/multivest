#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""让命令行脚本在 GBK 控制台上不因 emoji 崩掉。

背景
----
中文 Windows 的控制台默认代码页是 936（GBK）。`print("✅ 完成")` 里的 ✅
不在 GBK 里，`print` 会抛 UnicodeEncodeError。

麻烦的地方在于**崩的时机**：这些脚本都把汇总行放在最后，此时真正的活已经干完了
（exe 已经产出、文件已经写好），但用户看到的是 traceback 加非零退出码，
只能得出"失败了"的结论。实测 `tools/build_desktop.py` 就是这样 —— 44 MB 的
exe 正常生成，脚本却在最后一行汇总处崩掉。

处理方式
--------
优先把控制台输出代码页切到 UTF-8（中文与 emoji 都能正常显示）；
切换失败时退化为保留原编码、只把无法编码的字符替换成 `?`，保证脚本不中断。

用法
----
脚本开头 `import console  # noqa: F401` —— 导入即生效。
"""
from __future__ import annotations

import sys


def setup() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            # 65001 = UTF-8
            if ctypes.windll.kernel32.SetConsoleOutputCP(65001):
                for stream in (sys.stdout, sys.stderr):
                    try:
                        stream.reconfigure(encoding="utf-8")
                    except (AttributeError, ValueError, OSError):
                        pass
                return
        except Exception:  # noqa: BLE001 —— 拿不到控制台就往下走兜底
            pass

    # 兜底：保留控制台编码，仅避免"编不出就崩"
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


setup()
