<div align="center">

# ✂️ PDF-CropMaster

### 智能多核 PDF 批量自适应去白边利器 (每页独立裁剪)

**专治扫描件大白边与 CAD 复杂图纸！逐页独立智能识别 · 纯矢量无损 · 多核秒级导出 · 告别 WPS 单页手动点选**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-brightgreen.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)]()
[![Powered by PyMuPDF](https://img.shields.io/badge/engine-PyMuPDF-ff69b4.svg)](https://pymupdf.readthedocs.io/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/cai4415/pdf-cropmaster/pulls)

[English Documentation](README_EN.md) · [功能特性](#-核心特性) · [界面预览](#-界面预览) · [快速上手](#-快速上手) · [命令行使用](#-cli-命令行完整指南) · [核心原理解析](#-硬核原理解析)

</div>

---

## 📌 为什么开发这个工具？

在使用 WPS 或 Adobe Acrobat 等传统 PDF 工具裁剪页面时，我们常常陷入两大绝望困境：
1. **WPS「智能选区」只能单页手动点击**：一旦勾选“应用到全部页面”，软件并不会为每页智能计算，而是**强行拿当前第 1 页的裁剪框套死到所有页面**。如果文档各页排版稍有差异（如奇偶页边距、装配率图纸、规范汇编），内容就会被大面积裁掉或留下大片黑白边；
2. **扫描件与 CAD 导出图纸无法识别**：市面上的普通脚本仅根据文字块或单一图元做包围盒。但由 CAD 导出的扫描图纸往往由数十个横贯全页的图片切片拼接而成，图元包围盒直接等于全页尺寸，导致传统脚本**完全裁不掉任何白边**；
3. **重新采样破坏清晰度**：很多在线工具裁剪时会将 PDF 重新渲染成位图，导致**文字无法复制、无法搜索、体积暴增且画质变糊**。

**PDF-CropMaster 就是为了终结这一切痛点而生！**

---

## ✨ 核心特性

- 🎯 **每页独立自适应裁剪（Each-Page Auto Crop）**  
  打破全局模板限制！对 PDF 中的每一页独立计算真实内容边界（外接矩形），无论是 5 页合同还是 500 页图纸，全自动精准贴边。
- 🔍 **智能双引擎识别（矢量 + 极速像素扫描）**  
  - **矢量引擎**：聚合文字 Blocks、嵌入图片 Rects、矢量线条与绘图图元（Drawings，自动识别表格线、外框线），纯标量浮点极速求交；
  - **像素引擎**：自动识别“切片图铺满”的扫描件特征，无缝切换为 **72 DPI 灰度逐行极速扫描算法**（单页仅需 0.016 秒，比传统逐像素扫描快 75 倍！）。
- 💎 **100% 纯矢量无损保留**  
  仅修改 PDF 底层的 `CropBox` 视窗边界，**不重新采样图片、不改动矢量图元**。原汁原味，导出文件小，文本完全保留可选择、可复制、可检索特性。
- ⚡ **多进程多核全速并发**  
  突破 Python GIL 限制，自动压满多核 CPU 算力。基于独立 Worker 架构与抢占式任务调度，杜绝 Windows 多进程 spawn 冲突与长尾等待。
- 📊 **三种专业裁剪模式**  
  - `each`（默认）：每页自适应独立裁剪，紧凑极致；
  - `uniform`：四边按中位数统一尺寸，保持全书页面规格统一美观；
  - `union`：所有页面取最大并集尺寸，适合最保守的安全展示。
- 🖥️ **现代多端交互（GUI / Web / CLI 三合一）**  
  - **桌面客户端**：极简深色桌面 UI，支持多文件拖拽批量排队、独立小进度条、文件状态管理；
  - **网页端 Web UI**：内置轻量 Web 服务，浏览器零配置打开即可操作；
  - **命令行 CLI**：完备的参数配置，轻松嵌入自动化脚本与服务器定时任务。

---

## 📸 界面预览

### 现代桌面客户端 (Tkinter / TkinterDnD2)
> 支持直接拖拽多个 PDF 入队，每份文件拥有独立进度条与状态流转：

| 拖入文件排队待处理 | 极速多核批量裁剪完成 |
|:---:|:---:|
| ![排队待处理](assets/screenshots/01_queue_preview.png) | ![处理完成](assets/screenshots/02_completed_preview.png) |

---

## 🚀 快速上手

### 方式一：下载绿色免安装版 (Windows)

前往 [GitHub Releases](https://github.com/cai4415/pdf-cropmaster/releases) 下载最新版的 `PDF-CropMaster.exe`：
- **开箱即用**：无需配置 Python 环境，下载即可双击运行。
- **拖拽即裁**：直接将一个或多个 PDF 文件拖进窗口，点击「开始裁剪」即可。

### 方式二：源码直接运行 (跨平台 Windows / macOS / Linux)

#### 1. 克隆代码仓库
```bash
git clone https://github.com/cai4415/pdf-cropmaster.git
cd pdf-cropmaster
```

#### 2. 安装依赖
```bash
pip install -r requirements.txt
```

#### 3. 启动程序
- **启动桌面图形界面**：
  ```bash
  python main.py
  # 或者直接
  python auto_crop_pdf.py
  ```
- **启动轻量网页端界面**：
  ```bash
  python main.py --web-gui
  ```
- **命令行模式运行**：
  ```bash
  python main.py input.pdf
  ```

---

## 💻 CLI 命令行完整指南

PDF-CropMaster 提供极其灵活的命令行参数，适合批量脚本处理：

### 基础用法
```bash
# 最简用法：裁剪 input.pdf，自动输出 input_已裁剪.pdf
python auto_crop_pdf.py input.pdf

# 自定义输出路径
python auto_crop_pdf.py input.pdf -o output.pdf

# 自定义四周留白边距（默认 6mm）
python auto_crop_pdf.py input.pdf -m 10

# 使用磅（pt）作为边距单位
python auto_crop_pdf.py input.pdf --unit pt -m 15
```

### 高级裁剪模式
```bash
# 模式 1：每页各自独立计算边界（默认，最紧凑）
python auto_crop_pdf.py input.pdf --mode each

# 模式 2：取所有页面中位数统一规格（适合电子书排版）
python auto_crop_pdf.py input.pdf --mode uniform

# 模式 3：取所有页面并集（最保守，绝不裁破任何页）
python auto_crop_pdf.py input.pdf --mode union
```

### 局部裁剪与预检
```bash
# 只裁剪特定页码范围（1-20页、第35页、40-50页），其余页面保持原样
python auto_crop_pdf.py input.pdf --only 1-20,35,40-50

# 预检模式（只分析并打印各页识别尺寸，不写入实际文件）
python auto_crop_pdf.py input.pdf --dry-run

# 指定多进程核心数（0 为自动占用全部物理 CPU，1 为单进程）
python auto_crop_pdf.py input.pdf -j 8
```

### 参数一览表

| 参数 | 缩写 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `src` | - | *必填* | 输入 PDF 文件路径 |
| `--out` | `-o` | `<源名>_已裁剪.pdf` | 输出目标文件路径（自动防同名冲突覆盖） |
| `--margin` | `-m` | `6.0` | 裁剪后四周额外保留的安全边距 |
| `--unit` | - | `mm` | 边距单位：`mm`（毫米）或 `pt`（磅，1/72英寸） |
| `--mode` | - | `each` | 裁剪模式：`each` (独立) / `uniform` (中位数统一) / `union` (并集统一) |
| `--only` | - | 全部页面 | 指定只处理的页码范围，例如 `1-10,15,20-30` |
| `--workers` | `-j` | `0` (全核) | 并行扫描进程数（0 为自动检测所有核心） |
| `--dry-run` | - | `False` | 仅输出分析报告与尺寸数据，不生成文件 |
| `--web-gui` | - | - | 启动本地浏览器 Web 操作面板 |

---

## 🔬 硬核原理解析

### 1. 为什么纯矢量累加比 Rect 并集快一个数量级？
在包含复杂工程图纸或规范表格的页面中，单页往往包含 **数千至数万个** 矢量微图元（如剖面线、网格点）。  
传统做法若频繁调用 `out_rect |= new_rect`，每次都会在 Python 层创建新的 Rect 实例并计算交并集。  
**PDF-CropMaster** 改用纯标量（4 个 float 数值 `x0, y0, x1, y1`）在单次遍历中直接累积极值，性能提升整整一个数量级！

### 2. 为什么能秒破“CAD 扫描切片切图”？
CAD 导出的扫描 PDF（例如实测的 `1#楼.pdf`），常常由 60 多个横贯全页（如 `9918×272px`）的大图片条首尾相接拼接而成。  
- 若按图元包围盒计算：`union(img_rects)` 直接等于整张图纸尺寸，**裁剪量为 0**；
- **PDF-CropMaster 创新算法**：自动探测图元页面覆盖率（`coverage >= 92%`）。一旦命中扫描切片特征，立即自动降级为 **72 DPI 灰度 Pixmap 逐行扫描**。  
- 通过第一轮行级 `min(sample_row) < 245` 秒级定位上下边界，再针对有效行提取左右边界。实测一张 `1191×2105` 超大工程页面仅耗时 **0.016 秒**，彻底击穿白边！

### 3. 多进程并发为何独立拆分 `_pdfcrop_worker.py`？
Windows 平台的 `multiprocessing` 使用 `spawn` 机制，子进程启动时会重新导入入口模块。  
若将 Worker 函数写在主文件中，子进程重新导入时会误触界面初始化逻辑，造成子进程连环递归崩溃。将扫描内核彻底解耦为独立纯净的 Worker 模块，实现了真正的工业级防崩与全核并发。

---

## 🛠️ 本地打包 EXE (Windows)

本项目已提供自动化构建脚本与专属 Hook 配置（完美解决 `tkinterdnd2` 依赖的 DLL 和 Tcl 动态库缺失问题）：

```bash
# 1. 安装打包工具
pip install pyinstaller

# 2. 一键打包
python build_exe.py
```
构建完成后，产物将生成在 `dist/PDF-CropMaster.exe`。

---

## 📂 仓库项目结构

```
pdf-cropmaster/
├── assets/                     # 演示素材与界面截图
│   └── screenshots/
│       ├── 01_queue_preview.png
│       └── 02_completed_preview.png
├── auto_crop_pdf.py            # 核心主引擎（CLI / 桌面 GUI / Web GUI）
├── _pdfcrop_worker.py          # Windows 专属子进程多核并发扫描模块
├── main.py                     # 标准启动入口
├── build_exe.py                # 一键编译 Windows EXE 脚本
├── hook-tkinterdnd2.py         # PyInstaller 专用 Tcl/Tk 动态库 Hook
├── packaging/                  # 打包工程配置与 Spec
│   ├── hook-tkinterdnd2.py
│   └── pdf_cropmaster.spec
├── requirements.txt            # 核心运行依赖
├── requirements-dev.txt        # 开发与构建依赖
├── pyproject.toml              # 项目打包元数据
├── .gitignore                  # Git 忽略规则
├── LICENSE                     # MIT 开源许可证
├── README.md                   # 中文文档
└── README_EN.md                # 英文文档
```

---

## 🤝 参与贡献

欢迎提交 Issue、功能建议与 Pull Request！
1. Fork 本仓库
2. 创建您的特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交您的修改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 发起 Pull Request

---

## 📄 开源许可证

本项目基于 [MIT 许可证](LICENSE) 开源，欢迎自由用于个人或商业项目。
