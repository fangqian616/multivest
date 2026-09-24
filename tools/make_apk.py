#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建 Android 安装包（APK）—— 不依赖 Gradle。

为什么不用 Gradle
-----------------
Gradle 需要下载发行版本体（约 130 MB）并从 Maven 拉取 Android Gradle Plugin
与 AndroidX 依赖（又是上百 MB），第一次构建通常要十几分钟且容易卡在网络。
本项目的手机端只是一个 WebView 壳：一个 Activity、零第三方依赖。
对这种规模，直接用 SDK 自带的四个工具手工构建更快、更可控、更可复现：

    aapt2 compile    资源编译（res/ → .flat）
    aapt2 link       资源链接 + 生成 R.java + 产出未签名的 APK 骨架
    javac            编译 Java（含 R.java）
    d8               转成 classes.dex（DEX 字节码）
    zipalign         4 字节对齐（Android 对未对齐的 APK 会拒绝安装）
    apksigner        签名（未签名的 APK 无法安装）

前置条件：JDK + Android SDK（build-tools 与 platforms）。
`--bootstrap` 可以在缺失时自动下载安装 SDK 命令行工具与所需包。

用法
----
    python tools/make_apk.py                     # 用已安装的 SDK 构建
    python tools/make_apk.py --bootstrap         # 缺 SDK 时先自动安装
    python tools/make_apk.py --version 1.0.0
"""
from __future__ import annotations

import console  # noqa: F401  —— 见 tools/console.py：GBK 控制台下 emoji 不再中断脚本
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ANDROID = ROOT / "android"
DIST = ROOT / "dist"
APP_NAME_EN = "Multivest"

SDK_ROOT = Path(os.environ.get("ANDROID_SDK_ROOT")
                or os.environ.get("ANDROID_HOME")
                or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Android" / "Sdk")
BUILD_TOOLS_VERSION = "34.0.0"
PLATFORM_VERSION = "android-34"
CMDLINE_TOOLS_URL = ("https://dl.google.com/android/repository/"
                     "commandlinetools-win-11076708_latest.zip")

JAVA_HOME_CANDIDATES = [
    Path(r"C:\Program Files\Java\jdk-21"),
    Path(r"C:\Program Files\Java\jdk-17"),
    Path(r"C:\Program Files\Eclipse Adoptium"),
]


def _exe(name: str) -> str:
    return name + (".exe" if os.name == "nt" else "")


def find_java() -> tuple:
    """返回 (javac, keytool)。优先 JAVA_HOME，其次常见安装位置。"""
    jh = os.environ.get("JAVA_HOME")
    if jh:
        j = Path(jh) / "bin"
        if (j / _exe("javac")).is_file():
            return j / _exe("javac"), j / _exe("keytool")
    for c in JAVA_HOME_CANDIDATES:
        if (c / "bin" / _exe("javac")).is_file():
            return c / "bin" / _exe("javac"), c / "bin" / _exe("keytool")
    for d in Path(r"C:\Program Files\Java").glob("jdk*"):
        if (d / "bin" / _exe("javac")).is_file():
            return d / "bin" / _exe("javac"), d / "bin" / _exe("keytool")
    which = shutil.which("javac")
    if which:
        return Path(which), Path(shutil.which("keytool") or "keytool")
    print("❌ 找不到 JDK（需要 javac 与 keytool，JRE 不够）")
    raise SystemExit(1)


def find_tool(name: str) -> Path:
    """定位 build-tools 里的工具。

    **不能只试 `.exe`**：Android SDK 里 aapt2 / zipalign 是 .exe，
    而 d8 与 apksigner 是 .bat 包装脚本（它们内部调 java）。
    只按 .exe 找会漏掉后两个，报"找不到"，但文件其实就在同一个目录下。
    """
    suffixes = [".exe", ".bat", ""] if os.name == "nt" else [""]
    roots = [SDK_ROOT / "build-tools" / BUILD_TOOLS_VERSION]
    bt = SDK_ROOT / "build-tools"
    if bt.is_dir():
        roots += [d for d in sorted(bt.iterdir(), reverse=True)
                  if d.name != BUILD_TOOLS_VERSION]
    for root in roots:
        for sfx in suffixes:
            p = root / (name + sfx)
            if p.is_file():
                return p
    print(f"❌ 找不到 {name}（Android SDK build-tools）")
    print(f"   期望位置：{SDK_ROOT}\\build-tools\\{BUILD_TOOLS_VERSION}\\")
    print("   可加 --bootstrap 自动安装")
    raise SystemExit(1)


def android_jar() -> Path:
    p = SDK_ROOT / "platforms" / PLATFORM_VERSION / "android.jar"
    if p.is_file():
        return p
    pl = SDK_ROOT / "platforms"
    if pl.is_dir():
        for d in sorted(pl.iterdir(), reverse=True):
            q = d / "android.jar"
            if q.is_file():
                return q
    print(f"❌ 找不到 android.jar（{SDK_ROOT}\\platforms）")
    raise SystemExit(1)


def run(cmd: list, what: str, cwd: Path | None = None) -> None:
    r = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(f"  ❌ {what} 失败（exit {r.returncode}）")
        out = (r.stdout or "") + (r.stderr or "")
        print("     " + out.strip()[:1500].replace("\n", "\n     "))
        raise SystemExit(1)


def bootstrap() -> None:
    """下载并安装 SDK 命令行工具与所需包。"""
    if (SDK_ROOT / "build-tools").is_dir() and (SDK_ROOT / "platforms").is_dir():
        print(f"SDK 已就绪：{SDK_ROOT}")
        return
    print(f"正在准备 Android SDK → {SDK_ROOT}")
    cli = SDK_ROOT / "cmdline-tools" / "latest"
    if not (cli / "bin" / "sdkmanager.bat").is_file():
        cli.parent.mkdir(parents=True, exist_ok=True)
        zpath = Path(tempfile.gettempdir()) / "cmdline-tools.zip"
        print(f"  下载命令行工具（约 146 MB）… {CMDLINE_TOOLS_URL}")
        urllib.request.urlretrieve(CMDLINE_TOOLS_URL, zpath)
        tmp = cli.parent / "_tmp"
        if tmp.exists():
            shutil.rmtree(tmp)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(tmp)
        shutil.move(str(tmp / "cmdline-tools"), str(cli))
        shutil.rmtree(tmp, ignore_errors=True)
        zpath.unlink(missing_ok=True)
    sdk = cli / "bin" / "sdkmanager.bat"
    env = dict(os.environ, JAVA_HOME=str(find_java()[0].parents[1]))
    print("  接受许可 …")
    subprocess.run([str(sdk), f"--sdk_root={SDK_ROOT}", "--licenses"],
                   input="y\n" * 60, text=True, capture_output=True, env=env)
    print("  安装 platform-tools / platforms / build-tools …")
    r = subprocess.run([str(sdk), f"--sdk_root={SDK_ROOT}", "platform-tools",
                        f"platforms;{PLATFORM_VERSION}",
                        f"build-tools;{BUILD_TOOLS_VERSION}"],
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        print("  ❌ SDK 安装失败：", (r.stderr or r.stdout)[:600])
        raise SystemExit(1)
    print("  完成")


def make_icons(work: Path) -> None:
    """把项目图标转成各密度的启动图标。

    复用 frontend/assets 下已生成好的方形图标 —— 手机端与桌面端保持同一套视觉，
    也避免在构建脚本里再写一遍图标绘制逻辑。
    """
    from PIL import Image

    src = ROOT / "frontend" / "assets" / "icon-512.png"
    if not src.is_file():
        src = ROOT / "frontend" / "assets" / "icon-1024.png"
    if not src.is_file():
        print("  ⚠ 找不到图标源文件，跳过（将使用系统默认图标）")
        return
    # mdpi=48, hdpi=72, xhdpi=96, xxhdpi=144, xxxhdpi=192
    sizes = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
    im = Image.open(src).convert("RGBA")
    for name, px in sizes.items():
        d = work / "res" / f"mipmap-{name}"
        d.mkdir(parents=True, exist_ok=True)
        im.resize((px, px), Image.LANCZOS).save(d / "ic_launcher.png", "PNG")


def build(version: str, out: Path, keep_unsigned: bool) -> int:
    javac, keytool = find_java()
    aapt2 = find_tool("aapt2")
    d8 = find_tool("d8")
    zipalign = find_tool("zipalign")
    apksigner = find_tool("apksigner")
    ajar = android_jar()
    print(f"JDK     : {javac}")
    print(f"SDK     : {SDK_ROOT}")
    print(f"android : {ajar}")

    work = Path(tempfile.mkdtemp(prefix="multivest-apk-"))
    try:
        # ── 1. 资源准备（含图标）─────────────────────────────────────────
        shutil.copytree(ANDROID / "res", work / "res")
        make_icons(work)

        manifest = work / "AndroidManifest.xml"
        txt = (ANDROID / "AndroidManifest.xml").read_text(encoding="utf-8")
        txt = re.sub(r'android:versionName="[^"]*"',
                     f'android:versionName="{version}"', txt)
        manifest.write_text(txt, encoding="utf-8")

        # ── 2. aapt2 compile + link ───────────────────────────────────────
        print("  aapt2 compile …")
        flat = work / "res.zip"
        run([aapt2, "compile", "--dir", work / "res", "-o", flat], "aapt2 compile")

        print("  aapt2 link …")
        base_apk = work / "base.apk"
        gen = work / "gen"
        gen.mkdir(exist_ok=True)
        run([aapt2, "link", "-o", base_apk, "-I", ajar,
             "--manifest", manifest, "--java", gen,
             "--min-sdk-version", "24", "--target-sdk-version", "34",
             "--version-code", "1", "--version-name", version,
             "--auto-add-overlay", flat], "aapt2 link")

        # ── 3. javac ──────────────────────────────────────────────────────
        # 必须带 -g：d8 在读取缺少 SourceFile / LineNumberTable 属性的类文件时
        # 会抛 NullPointerException 并以 "internal error" 结束（实测匿名内部类
        # MainActivity$1.class 触发）。带上调试信息既解决该问题，也便于崩溃时定位。
        print("  javac …")
        classes = work / "classes"
        classes.mkdir(exist_ok=True)
        sources = sorted(str(p) for p in (ANDROID / "java").rglob("*.java"))
        sources += sorted(str(p) for p in gen.rglob("*.java"))
        if not sources:
            print("  ❌ 没有找到 Java 源文件")
            return 1
        run([javac, "-g", "-source", "8", "-target", "8", "-nowarn",
             "-classpath", ajar, "-d", classes] + sources, "javac")

        # ── 4. d8 → classes.dex ───────────────────────────────────────────
        print("  d8 → classes.dex …")
        dexdir = work / "dex"
        dexdir.mkdir(exist_ok=True)
        class_files = sorted(str(p) for p in classes.rglob("*.class"))
        run([d8, "--lib", ajar, "--min-api", "24", "--output", dexdir]
            + class_files, "d8")

        # ── 5. 把 dex 塞进 APK ────────────────────────────────────────────
        print("  打包 dex …")
        unsigned = work / "unsigned.apk"
        shutil.copy2(base_apk, unsigned)
        with zipfile.ZipFile(unsigned, "a", zipfile.ZIP_DEFLATED) as z:
            z.write(dexdir / "classes.dex", "classes.dex")
        if keep_unsigned:
            dst = out.with_name(out.stem + "-unsigned.apk")
            shutil.copy2(unsigned, dst)
            print(f"  已保留未签名版本：{dst}")

        # ── 6. zipalign（必须在校验之后、签名之前）────────────────────────
        print("  zipalign …")
        aligned = work / "aligned.apk"
        run([zipalign, "-f", "-p", "4", unsigned, aligned], "zipalign")

        # ── 7. 签名 ───────────────────────────────────────────────────────
        ks = ROOT / "android" / "keystore" / "multivest.jks"
        ks.parent.mkdir(parents=True, exist_ok=True)
        if not ks.is_file():
            print("  生成签名密钥（首次构建）…")
            run([keytool, "-genkeypair", "-v",
                 "-keystore", ks, "-alias", "multivest",
                 "-keyalg", "RSA", "-keysize", "2048", "-validity", "10950",
                 "-storepass", "multivest", "-keypass", "multivest",
                 "-dname", "CN=Multivest, OU=Research, O=fangqian616, C=CN"],
                "keytool")
            print(f"     keystore: {ks}")
            print("     口令与别名为 multivest（本地测试用；正式发布请替换）")

        print("  apksigner …")
        out.parent.mkdir(parents=True, exist_ok=True)
        run([apksigner, "sign", "--ks", ks, "--ks-key-alias", "multivest",
             "--ks-pass", "pass:multivest", "--key-pass", "pass:multivest",
             "--v1-signing-enabled", "true", "--v2-signing-enabled", "true",
             "--out", out, aligned], "apksigner")

        print("  apksigner verify …")
        run([apksigner, "verify", "--verbose", out], "apksigner verify")

        size_mb = out.stat().st_size / 1024 / 1024
        print()
        print("=" * 60)
        print(f"  ✅ {out.name}")
        print("=" * 60)
        print(f"  路径 : {out}")
        print(f"  体积 : {size_mb:.2f} MB")
        print(f"  版本 : {version}   minSdk 24 (Android 7.0)   targetSdk 34")
        print("  安装 : 传到手机点击安装（需允许「未知来源」）")
        print("  用法 : 打开后填电脑「连接」页显示的地址，如 http://192.168.1.5:8760")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="1.0.0")
    ap.add_argument("--out", default=None)
    ap.add_argument("--bootstrap", action="store_true",
                    help="SDK 缺失时自动下载安装")
    ap.add_argument("--keep-unsigned", action="store_true")
    args = ap.parse_args()

    if args.bootstrap:
        bootstrap()
    out = Path(args.out) if args.out else DIST / f"{APP_NAME_EN}-{args.version}.apk"
    return build(args.version, out, args.keep_unsigned)


if __name__ == "__main__":
    sys.exit(main())
