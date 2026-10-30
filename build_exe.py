#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键构建 PDF-CropMaster 单文件独立 EXE 打包脚本。

运行方式：
    python build_exe.py

产物位置：
    dist/PDF-CropMaster.exe
"""

import os
import subprocess
import sys


def build():
    print("=" * 60)
    print("开始构建 PDF-CropMaster 独立单文件可执行程序...")
    print("=" * 60)

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[错误] 未检测到 PyInstaller，请先运行: pip install pyinstaller")
        sys.exit(1)

    project_root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(project_root)

    spec_file = os.path.join(project_root, "packaging", "pdf_cropmaster.spec")
    if os.path.isfile(spec_file):
        cmd = [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm",
            "--clean",
            spec_file,
        ]
    else:
        cmd = [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--windowed",
            "--specpath", "build",
            "--name", "PDF-CropMaster",
            "--hidden-import", "_pdfcrop_worker",
            "--additional-hooks-dir", ".",
            "--collect-all", "pymupdf",
            "auto_crop_pdf.py",
        ]

    print("执行命令:", " ".join(cmd))
    ret = subprocess.call(cmd)
    if ret == 0:
        dist_exe = os.path.join(project_root, "dist", "PDF-CropMaster.exe")
        print("\n" + "=" * 60)
        print("构建成功！")
        if os.path.exists(dist_exe):
            size_mb = os.path.getsize(dist_exe) / (1024 * 1024)
            print(f"输出文件: {dist_exe}")
            print(f"文件大小: {size_mb:.2f} MB")
        print("=" * 60)
    else:
        print(f"\n构建失败，退出码: {ret}")
        sys.exit(ret)


if __name__ == "__main__":
    build()
