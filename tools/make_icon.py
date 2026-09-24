#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「智能多维投资系统」应用图标。

设计：红色渐变圆角方 + 白色递升柱 + 金色趋势线与顶点。
不用任何传统元素；就是一枚干净的现代金融标识 ——
柱状代表「多维」（多个资产维度），折线代表「趋势与研判」。

配色：红 #D92B2B→#AE1F1F ／ 白 #FFFFFF ／ 金 #F0C24A

产出：
    frontend/assets/icon-{192,512,1024}.png
    frontend/assets/icon-rounded.png
    frontend/assets/icon.ico
"""
from __future__ import annotations

import console  # noqa: F401  —— 见 tools/console.py：GBK 控制台下 emoji 不再中断脚本
import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "frontend" / "assets"
OUT.mkdir(parents=True, exist_ok=True)

S = 1024
RED_TOP = (226, 56, 56)
RED_BOT = (166, 28, 28)
WHITE = (255, 255, 255)
GOLD = (240, 194, 74)


def lerp(a, b, t):
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def rounded_mask(size: int, ratio: float) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1],
                                        radius=int(size * ratio), fill=255)
    return m


def build(size: int) -> Image.Image:
    # 底：垂直渐变红
    base = Image.new("RGB", (S, S), RED_TOP)
    bd = ImageDraw.Draw(base)
    for y in range(S):
        bd.line([(0, y), (S, y)], fill=lerp(RED_TOP, RED_BOT, y / (S - 1)))

    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(base, (0, 0), rounded_mask(S, 0.215))

    d = ImageDraw.Draw(img, "RGBA")

    # ── 三根递升柱（表示「多维」）──
    n = 3
    bar_w = S * 0.105
    gap = S * 0.062
    total = n * bar_w + (n - 1) * gap
    x0 = (S - total) / 2 - S * 0.055
    base_y = S * 0.735
    heights = [0.175, 0.285, 0.395]
    alphas = [140, 190, 255]
    for i, (h, al) in enumerate(zip(heights, alphas)):
        x = x0 + i * (bar_w + gap)
        top = base_y - S * h
        d.rounded_rectangle([x, top, x + bar_w, base_y],
                            radius=int(bar_w * 0.30), fill=WHITE + (al,))

    # ── 金色趋势线 + 顶点 ──
    pts = [
        (x0 + bar_w * 0.5, base_y - S * 0.175 - S * 0.055),
        (x0 + bar_w + gap + bar_w * 0.5, base_y - S * 0.285 - S * 0.055),
        (x0 + 2 * (bar_w + gap) + bar_w * 0.5, base_y - S * 0.395 - S * 0.055),
    ]
    lw = int(S * 0.028)
    d.line(pts, fill=GOLD, width=lw, joint="curve")
    for p in pts:
        r = S * 0.026
        d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=GOLD)

    # 顶点额外一圈
    tip = (pts[-1][0] + S * 0.075, pts[-1][1] - S * 0.085)
    d.line([pts[-1], tip], fill=GOLD, width=lw, joint="curve")
    r = S * 0.034
    d.ellipse([tip[0] - r, tip[1] - r, tip[0] + r, tip[1] + r], fill=WHITE)
    d.ellipse([tip[0] - r * 0.5, tip[1] - r * 0.5,
               tip[0] + r * 0.5, tip[1] + r * 0.5], fill=GOLD)

    # 顶部一抹高光，避免纯平
    gloss = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(gloss)
    for y in range(int(S * 0.5)):
        a = int(38 * (1 - y / (S * 0.5)))
        gd.line([(0, y), (S, y)], fill=(255, 255, 255, a))
    gloss.putalpha(Image.composite(gloss.split()[3], Image.new("L", (S, S), 0),
                                   rounded_mask(S, 0.215)))
    img = Image.alpha_composite(img, gloss)

    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    for size in (192, 512, 1024):
        p = OUT / f"icon-{size}.png"
        build(size).save(p, "PNG", optimize=True)
        print(f"  ✓ {p}  ({p.stat().st_size / 1024:.1f} KB)")

    sq = build(512)
    sq.save(OUT / "icon-rounded.png", "PNG", optimize=True)
    print(f"  ✓ {OUT / 'icon-rounded.png'}")
    sq.save(OUT / "icon.ico",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"  ✓ {OUT / 'icon.ico'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
