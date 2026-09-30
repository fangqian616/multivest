#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态检查前端：调用但未定义的函数、引用但不存在的 DOM id。

为什么需要这个
--------------
撤下诊断面板时按「注释块 → 注释块」整段删除，把夹在中间的
`renderDailyHistory()` 一起删了 —— **函数没了、调用还在**，
于是 loadDaily() 抛 ReferenceError，整个「智能研判」页白掉，
表现成「报告生成不出来」。而 `node --check` 只验语法，验不出未定义标识符。

这个脚本就是来补这个缺口的。它自己踩过的坑也记在下面，避免下次误报淹没真问题：

  · 逗号分隔声明：`const g = ..., gn = ...` —— 只抓第一个标识符会漏掉后面全部
  · 模板字符串里的 CSS：`var(--x)` / `url(...)` / `rotate(...)` 会被当成函数调用
  · `async function` 的 `async` 会被当成调用
  · 回调形参（`done` / `ok` / `cb`）不是未定义，是参数
  · id 可以在运行时创建：`el.id = 'prg-rerun'` 也算已定义

用法：
    python tools/check_frontend.py        # 0 = 干净
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = ROOT / "frontend" / "app.js"
CHARTS = ROOT / "frontend" / "charts.js"
HTML = ROOT / "frontend" / "index.html"

# JS 关键字与全局对象（不是"未定义函数"）
KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "typeof", "function",
    "new", "await", "else", "do", "in", "of", "async", "delete", "void",
    "yield", "case", "throw", "try", "finally", "class", "extends", "with",
    "instanceof", "super", "this", "import", "export", "default",
}
GLOBALS = {
    "parseInt", "parseFloat", "isNaN", "isFinite", "encodeURIComponent",
    "decodeURIComponent", "setTimeout", "clearTimeout", "setInterval",
    "clearInterval", "requestAnimationFrame", "cancelAnimationFrame",
    "fetch", "alert", "confirm", "prompt", "btoa", "atob", "structuredClone",
    "queueMicrotask", "getComputedStyle", "matchMedia",
    "String", "Number", "Boolean", "Array", "Object", "JSON", "Math", "Date",
    "Map", "Set", "WeakMap", "Promise", "Error", "TypeError", "RegExp",
    "Symbol", "BigInt", "Intl", "URL", "URLSearchParams", "Blob", "FileReader",
    "FormData", "EventSource", "WebSocket", "AbortController", "TextEncoder",
    "TextDecoder", "Image", "Audio", "ResizeObserver", "IntersectionObserver",
    "MutationObserver", "CustomEvent", "Event", "Node", "Element", "Range",
    "requestIdleCallback", "localStorage", "sessionStorage", "document",
    "window", "console", "navigator", "location", "history", "performance",
}
# 模板字符串里的 CSS 函数
CSS_FUNCS = {
    "var", "url", "rotate", "translate", "translateX", "translateY", "scale",
    "scaleX", "scaleY", "calc", "rgb", "rgba", "hsl", "hsla", "min", "max",
    "clamp", "attr", "counter", "format", "local", "env", "cubic-bezier",
    "steps", "repeat", "matrix", "skew", "perspective", "drop-shadow",
    "linear-gradient", "radial-gradient", "circle", "ellipse", "inset", "path",
    "polygon", "blur", "brightness", "contrast", "saturate", "grayscale",
}
SKIP = KEYWORDS | GLOBALS | CSS_FUNCS


def strip_literals(src: str) -> str:
    """把字符串/模板/注释替换成等长空白，避免把里面的 CSS 当成代码。

    保留换行，行号才不会漂。
    """
    out = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif ch == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join(c if c == "\n" else " " for c in src[i:j]))
            i = j
        elif ch in "'\"`":
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == ch:
                    break
                j += 1
            j = min(j + 1, n)
            out.append("".join(c if c == "\n" else " " for c in src[i:j]))
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def declared(code: str) -> set:
    """所有声明过的标识符：函数名、const/let/var、形参。

    逗号声明必须按「, ident =」逐个抓，不能只取语句的第一个 ——
    `const g = ..., gn = ...` 就是这么把 gn 漏掉的。
    """
    names = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", code))
    # 单个或逗号分隔的声明：const|let|var X = ... , Y = ...
    names |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", code))
    names |= set(re.findall(r",\s*([A-Za-z_$][\w$]*)\s*=", code))
    # 解构：const { a, b } = ... / const [a, b] = ...
    for m in re.finditer(r"(?:const|let|var)\s*[{\[]([^}\]]*)[}\]]", code):
        for d in re.finditer(r"([A-Za-z_$][\w$]*)", m.group(1)):
            names.add(d.group(1))
    # 形参：function f(a, b) / (a, b) => ... / function (a, b) {
    for m in re.finditer(r"(?:function\s*[\w$]*\s*)?\(([^)]*)\)\s*(?:=>|\{)", code):
        for p in re.finditer(r"([A-Za-z_$][\w$]*)", m.group(1)):
            names.add(p.group(1))
    return names


def main() -> int:
    js_raw = JS.read_text(encoding="utf-8")
    charts = CHARTS.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")

    js = strip_literals(js_raw)
    charts_code = strip_literals(charts)

    defined = declared(js_raw) | declared(charts)
    defined |= {"Charts"}
    defined |= set(re.findall(r"^\s*([A-Za-z_$][\w$]*)\s*[:(]", charts, re.M))

    called = set(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", js))
    unknown = sorted(c for c in called if c not in defined and c not in SKIP)

    print("=== 调用但未定义的函数 ===")
    if unknown:
        for name in unknown:
            lines = [i + 1 for i, ln in enumerate(js.splitlines())
                     if re.search(rf"(?<![.\w$]){re.escape(name)}\s*\(", ln)]
            print(f"  ❌ {name}()  第 {lines[:8]} 行")
    else:
        print("  ✅ 无")

    # DOM id：允许运行时创建（el.id = 'x'）
    ids_html = set(re.findall(r'id="([^"]+)"', html))
    ids_dynamic = set(re.findall(r"\.id\s*=\s*['\"]([\w-]+)['\"]", js))
    ids_js = set(re.findall(r"\$\('#([\w-]+)'\)", js)) | set(
        re.findall(r"getElementById\(['\"]([\w-]+)['\"]\)", js))
    miss = sorted(i for i in ids_js if i not in ids_html and i not in ids_dynamic)

    print("\n=== 引用但不存在的 DOM id ===")
    if miss:
        for name in miss:
            lines = [i + 1 for i, ln in enumerate(js.splitlines())
                     if f"'{name}'" in ln]
            print(f"  ❌ #{name}  第 {lines[:6]} 行")
    else:
        print("  ✅ 无")

    ok = not unknown and not miss
    print(f"\n{'✅ 前端静态检查通过' if ok else '❌ 存在缺失定义'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
