#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PDF-CropMaster (智能多核 PDF 批量裁剪工具) 快速启动入口。

- 直接运行：启动桌面现代化 GUI 窗口（支持拖拽多个 PDF 批量排队处理）
- 命令行运行：传入参数（如 python main.py input.pdf）自动切换为 CLI 模式
- 网页模式：传入 --web-gui 启动轻量级本地网页端界面
"""

import sys
import multiprocessing
from auto_crop_pdf import main, run_gui, run_web_gui, run_tk_gui, gui_selftest

if __name__ == "__main__":
    try:
        multiprocessing.freeze_support()
    except Exception:
        pass

    if any(a.startswith("--gui-selftest-list") for a in sys.argv[1:]):
        d = ""
        for a in sys.argv[1:]:
            if a.startswith("--gui-selftest-list="):
                d = a.split("=", 1)[1].strip('"')
        run_tk_gui(d)
        sys.exit(0)

    if any(a.startswith("--gui-selftest") for a in sys.argv[1:]):
        sys.exit(gui_selftest())

    if "--web-gui" in sys.argv[1:]:
        run_web_gui()
        sys.exit(0)

    if len(sys.argv) > 1:
        main()
    else:
        run_gui()
