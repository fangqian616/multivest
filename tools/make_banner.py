#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 GitHub 仓库顶部的 banner 图。

参考了同类开源项目（如易标投标工具箱）的做法：README 顶部放一张宽幅 banner，
把「是什么 / 一句话卖点 / 关键数字」在第一屏说清楚。

输出：docs/screenshots/banner.png（1280×400）

设计沿用项目自身的设计系统（红·白·金），不引入新配色：
  · 左侧：应用图标（红色圆角方 + 三根递升柱 + 金色趋势线）
  · 右侧：产品名 + 中文名 + 一句话定位 + 三个关键数字
"""
from __future__ import annotations

import pathlib

from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSETS = ROOT / "frontend" / "assets"
OUT = ROOT / "docs" / "screenshots" / "banner.png"

W, H = 1280, 400
BG = (246, 247, 249)
INK = (15, 19, 25)
INK2 = (74, 84, 98)
INK3 = (138, 147, 163)
RED = (217, 43, 43)
GOLD = (200, 160, 46)
LINE = (230, 232, 236)

F_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
F_REG = "C:/Windows/Fonts/msyh.ttc"
F_NUM = "C:/Windows/Fonts/consola.ttf"


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)

    # 顶部一条红色细线，呼应设计系统里的「红」只用于强调
    d.rectangle([0, 0, W, 5], fill=RED)

    # ── 左侧图标 ──────────────────────────────────────────────────────
    icon_p = ASSETS / "icon-512.png"
    if icon_p.is_file():
        icon = Image.open(icon_p).convert("RGBA").resize((148, 148), Image.LANCZOS)
        im.paste(icon, (72, 126), icon)

    # ── 右侧文字 ──────────────────────────────────────────────────────
    x = 268
    f_title = ImageFont.truetype(F_BOLD, 62)
    f_cn = ImageFont.truetype(F_REG, 30)
    f_tag = ImageFont.truetype(F_REG, 19)
    f_num = ImageFont.truetype(F_NUM, 26)
    f_lab = ImageFont.truetype(F_REG, 15)

    d.text((x, 92), "Multivest", font=f_title, fill=INK)
    d.text((x + 4, 168), "智能多维投资系统", font=f_cn, fill=INK2)
    d.text((x + 4, 214), "多智能体量化投研会的家庭资产配置与每日行情研判工具",
           font=f_tag, fill=INK3)

    # ── 三个关键数字 ───────────────────────────────────────────────────
    d.line([x + 2, 268, 1160, 268], fill=LINE, width=1)
    nums = [
        ("5", "位分析师 + 风控 + 委员会", RED),
        ("6", "维确定性评分（模型只做有界调整）", GOLD),
        ("69s", "每晚一次完整研判 · 28 次模型调用", INK),
    ]
    cx = x
    for big, lab, col in nums:
        d.text((cx, 288), big, font=f_num, fill=col)
        bw = d.textlength(big, font=f_num)
        d.text((cx + bw + 10, 297), lab, font=f_lab, fill=INK3)
        cx += bw + 10 + d.textlength(lab, font=f_lab) + 44

    # ── 右下角：诚实标签（这个项目的差异点） ─────────────────────────────
    tag = "量化预测仅参考 · 谨慎决策"
    tw = d.textlength(tag, font=f_tag)
    d.text((1160 - tw, 348), tag, font=f_tag, fill=GOLD)

    im.save(OUT, "PNG", optimize=True)
    print(f"已生成 {OUT}  ({OUT.stat().st_size/1024:.0f} KB, {W}x{H})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
