# -*- coding: utf-8 -*-
"""PyInstaller hook：收集 tkinterdnd2 的 tkdnd Tcl 脚本 + DLL。

为什么自己写：
  官方 hooks-contrib 里**没有** tkinterdnd2 的 hook。
  而 collect_data_files() 默认**不收集 .dll**（二进制扩展被排除），
  只写它会导致 exe 启动即报 "Unable to load tkdnd library"，
  且日志里看不出缺什么。

过滤逻辑只保留 Windows 目录，并且**保留根目录下的 Tcl 脚本**
（pkgIndex.tcl 等，没有平台名，不能被误删）；过滤结果为空则全量回退，
宁可包体大 10MB 也不能跑不起来。
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

# 所有平台目录名（tkinterdnd2 0.6.x 的 tkdnd 布局）
PLATFORMS = {
    "linux-arm64", "linux-arm64-tcl9",
    "linux-x64", "linux-x64-tcl9",
    "osx-arm64", "osx-arm64-tcl9",
    "osx-x64",
    "win-arm64",
    "win-x64", "win-x64-tcl9",      # Tcl 8.6 / Tcl 9
    "win-x86", "win-x86-tcl9",
}
KEEP = {"win-x64", "win-x64-tcl9"}   # 本机 Tcl 8.6 → win-x64；tcl9 版本留着备用


def _pick(items):
    out = []
    for src, dest in items:
        parts = set(str(dest).replace("\\", "/").split("/"))
        plat = parts & PLATFORMS
        if plat and not (plat & KEEP):
            continue          # 是别的平台目录 → 丢弃
        out.append((src, dest))  # 根目录文件（无平台名）或目标平台 → 保留
    return out


datas = _pick(collect_data_files("tkinterdnd2"))
binaries = _pick(collect_dynamic_libs("tkinterdnd2"))

if not binaries:   # 一个 DLL 都没收到，说明包结构变了，全量回退
    datas = collect_data_files("tkinterdnd2")
    binaries = collect_dynamic_libs("tkinterdnd2")
