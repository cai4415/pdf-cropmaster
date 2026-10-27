#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PDF 每页独立智能裁剪（去白边）—— 替代 WPS "智能选区" 只能单页点的问题

原理：逐页扫描文字块 / 图片 / 矢量图元，算出这一页真实内容的最小外接矩形，
      再往外扩一个固定边距，写回该页的 CropBox。全程矢量操作，不重新采样图片，
      文字仍可搜索、可复制。

用法：
  python auto_crop_pdf.py 输入.pdf                      # 输出 输入_已裁剪.pdf，每页各自裁剪
  python auto_crop_pdf.py 输入.pdf -o 输出.pdf
  python auto_crop_pdf.py 输入.pdf -m 8                 # 边距 8 毫米（默认 6）
  python auto_crop_pdf.py 输入.pdf --unit pt --m 15    # 边距用磅
  python auto_crop_pdf.py 输入.pdf --mode uniform       # 所有页面统一成一个框（取中位数）
  python auto_crop_pdf.py 输入.pdf --mode union         # 所有页面统一成一个框（取并集，最保守）
  python auto_crop_pdf.py 输入.pdf --only 1-20,35,40-50  # 只裁指定页，其余原样保留
  python auto_crop_pdf.py 输入.pdf --dry-run            # 只报告每页裁剪结果，不写文件
"""

import argparse
import os
import sys
import time

try:
    import pymupdf as fitz  # PyMuPDF 新包名
except ImportError:
    try:
        import fitz  # 旧版包名
    except ImportError:
        sys.exit("缺少 PyMuPDF。安装：pip install pymupdf")


MM_TO_PT = 72.0 / 25.4
DARK_THRESHOLD = 245   # 灰度低于此值算“有内容”，用于识别扫描件白边
PAGE_COVER_RATIO = 0.92  # 图元覆盖超过此比例 → 判定为扫描件，改用像素法


# ---------------------------------------------------------------- 边界计算

def pixel_content_rect(page, dpi=72, threshold=DARK_THRESHOLD):
    """按像素内容判断真实边界（等同WPS「智能选区」的思路）。

    为什么需要这条路径：
    CAD 导出的扫描图纸，页面常被切成大量横贯整页的图片条
    （实测「1#楼.pdf」每页 65 个 9918x272 的图片条首尾相接铺满全页）。
    按图元位置算并集结果 = 整个页面 → 裁不掉任何东西。
    只有看像素，才能识别真正的白边。

    性能：灰度 pixmap + 逐行 min()，实测 1191x2105 页仅 0.016 秒。
    （逐像素三层通道比较是它的 75 倍慢，不要用那种写法）
    """
    try:
        pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    except Exception:
        return None
    if pix.width < 2 or pix.height < 2:
        return None

    sm, st, w, h = pix.samples, pix.stride, pix.width, pix.height

    # 第一次扫：找出有内容的行（上下边界）
    top, bot = -1, -1
    for y in range(h):
        if min(sm[y * st:y * st + st]) < threshold:
            if top < 0:
                top = y
            bot = y
    if top < 0:
        return None  # 全白页

    # 第二次扫：只在有内容的行里找左右边界
    left, right = w, -1
    for y in range(top, bot + 1):
        row = sm[y * st:y * st + st]
        for x in range(w):
            if row[x] < threshold:
                if x < left:
                    left = x
                break
        for x in range(w - 1, -1, -1):
            if row[x] < threshold:
                if x > right:
                    right = x
                break

    if right < 0 or right <= left:
        return None

    pr = page.rect
    sx = pr.width / w
    sy = pr.height / h
    return fitz.Rect(left * sx, top * sy, (right + 1) * sx, (bot + 1) * sy)


def _covers_whole_page(page, min_pt=1.0):
    """判断页面的图元/文字是否几乎铺满整页（扫描件特征）。"""
    pr = page.rect
    page_area = pr.width * pr.height
    if page_area <= 0:
        return False

    x0 = y0 = 1e30
    x1 = y1 = -1e30
    hit = False

    try:
        for info in page.get_images(full=True):
            for r in page.get_image_rects(info[0]):
                if not (r.is_valid and r.width > 0 and r.height > 0):
                    continue
                if r.x0 < x0:
                    x0 = r.x0
                if r.y0 < y0:
                    y0 = r.y0
                if r.x1 > x1:
                    x1 = r.x1
                if r.y1 > y1:
                    y1 = r.y1
                hit = True
    except Exception:
        pass
    try:
        for d in page.get_drawings():
            r = d.get("rect")
            if r is None or not r.is_valid or r.width < min_pt or r.height < min_pt:
                continue
            if r.x0 < x0:
                x0 = r.x0
            if r.y0 < y0:
                y0 = r.y0
            if r.x1 > x1:
                x1 = r.x1
            if r.y1 > y1:
                y1 = r.y1
            hit = True
    except Exception:
        pass

    if not hit:
        return False
    cover = (x1 - x0) * (y1 - y0) / page_area
    return cover >= PAGE_COVER_RATIO


def page_content_rect_auto(page, min_pt=1.0, dpi=72):
    """自动选择边界识别策略。

    先按图元位置算；如果发现图元几乎铺满整页（扫描件特征），
    改用像素法重算，否则会把整页当成内容导致裁不掉。
    """
    rect = page_content_rect(page, min_pt)
    pr = page.rect
    if rect is None:
        # 图元层没检出，但可能有扫描图 → 交给像素法判断
        return pixel_content_rect(page, dpi)

    # 图元包围盒接近整页 → 多半是扫描件，切片图铺满，需像素法
    cover = (rect.width * rect.height) / (pr.width * pr.height) \
        if pr.width and pr.height else 0
    if cover >= PAGE_COVER_RATIO:
        pix = pixel_content_rect(page, dpi)
        if pix is not None:
            return pix
    return rect


# ---------------------------------------------------------------- 边界计算

def page_content_rect(page, min_pt=1.0):
    """算出这一页真实内容的最小外接矩形。返回 None 表示整页空白。

    性能要点：全程用纯数值(float)累积包围盒，不用 Rect 对象做逐个合并运算。
    复杂图纸单页可达数千个图元，用 `out |= r` 逐个合并是主要瓶颈，
    改成比较四个标量后快一个数量级。
    """
    x0 = y0 = 1e30
    x1 = y1 = -1e30
    hit = False

    # 1) 文字（blocks 已含多行合并，比 words 稳）
    try:
        for b in page.get_text("blocks"):
            bx0, by0, bx1, by1 = b[0], b[1], b[2], b[3]
            if bx1 > bx0 and by1 > by0:
                if bx0 < x0:
                    x0 = bx0
                if by0 < y0:
                    y0 = by0
                if bx1 > x1:
                    x1 = bx1
                if by1 > y1:
                    y1 = by1
                hit = True
    except Exception:
        pass

    # 2) 图片
    try:
        for info in page.get_images(full=True):
            for r in page.get_image_rects(info[0]):
                if r.is_valid and r.width > 0 and r.height > 0:
                    if r.x0 < x0:
                        x0 = r.x0
                    if r.y0 < y0:
                        y0 = r.y0
                    if r.x1 > x1:
                        x1 = r.x1
                    if r.y1 > y1:
                        y1 = r.y1
                    hit = True
    except Exception:
        pass

    # 3) 矢量图元（表格线、线条、填充块）——CAD/规范文件里表格线是主要边界来源
    try:
        for d in page.get_drawings():
            r = d.get("rect")
            if r is None or not r.is_valid:
                continue
            if r.width < min_pt or r.height < min_pt:
                continue
            if r.x0 < x0:
                x0 = r.x0
            if r.y0 < y0:
                y0 = r.y0
            if r.x1 > x1:
                x1 = r.x1
            if r.y1 > y1:
                y1 = r.y1
            hit = True
    except Exception:
        pass

    if not hit or x1 <= x0 or y1 <= y0:
        return None
    return fitz.Rect(x0, y0, x1, y1)


# ---------------------------------------------------------------- 并行扫描

def _scan_chunk(args):
    """转发到独立 worker 模块。真正的实现在 _pdfcrop_worker.py。

    必须转发而不是直接实现：Windows spawn 子进程会重新导入主模块，
    worker 放主文件里会重复执行界面逻辑。
    """
    from _pdfcrop_worker import scan_chunk
    return scan_chunk(args)


def scan_pages_parallel(src, n, workers=0, min_pt=1.0, progress=None):
    """并行扫描全部页面内容框。返回 {页码: Rect}。"""
    import multiprocessing as mp

    # 用满所有物理核心。之前硬编码上限 8，在16 核机上等于白扔一半算力。
    if workers <= 0:
        workers = os.cpu_count() or 4
    workers = max(1, min(workers, n))

    # 单页或页数很少时，进程开销大于收益，直接串行
    if workers == 1 or n <= 2:
        doc = fitz.open(src)
        try:
            content, blanks = {}, []
            for i in range(n):
                r = page_content_rect_auto(doc[i], min_pt)
                (content.setdefault(i, r) if r is not None else None)
                if r is None:
                    blanks.append(i + 1)
                if progress:
                    progress(i + 1, n)
            return content, blanks
        finally:
            doc.close()

    # 每页一个任务，imaps 让空闲进程自动抢下一页。
    # 实测 fitz.open 仅 1.2ms，拆到单页几乎无成本，却能彻底消除拖尾
    # （原先均分 8 段，最后一段只有 1 页，其余进程干等）。
    tasks = [(src, s, s + 1, min_pt) for s in range(n)]
    pool_size = min(workers, n)

    content, blanks = {}, []
    done = 0

    # Windows 没有 fork，spawn 子进程需要能重新导入主模块。
    # 在 stdin / 冻结(exe) 等环境下会反复报
    # OSError: Invalid argument '<stdin>' 导致子进程连环崩溃，
    # 因此这里必须 try/except 兜底，失败就退回串行，宁可慢也不能崩。
    ctx = mp.get_context("spawn")

    try:
        with ctx.Pool(processes=pool_size) as pool:
            for part in pool.imap_unordered(_scan_chunk, tasks):
                for idx, box in part:
                    done += 1
                    if box is None:
                        blanks.append(idx + 1)
                    else:
                        content[idx] = fitz.Rect(*box)
                    if progress:
                        progress(done, n)
    except Exception as e:
        import traceback
        print(f"[提示] 并行不可用：{type(e).__name__}: {e}")
        traceback.print_exc()
        return scan_pages_parallel(src, n, workers=1, min_pt=min_pt,
                                   progress=progress)

    blanks.sort()
    return content, blanks


def parse_page_spec(spec, n):
    """把 '1-20,35,40-50' 解析成 0 基页码集合。"""
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a, b = int(a), int(b)
            a, b = int(a), int(b)
            lo, hi = min(a, b), max(a, b)
            for p in range(lo, hi + 1):
                if 1 <= p <= n:
                    out.add(p - 1)
        else:
            p = int(part)
            if 1 <= p <= n:
                out.add(p - 1)
    return out


def median_rect(rects):
    """取各页内容框四条边的中位数，得到一个"大家都适用"的统一框。"""
    xs0 = sorted(r.x0 for r in rects)
    ys0 = sorted(r.y0 for r in rects)
    xs1 = sorted(r.x1 for r in rects)
    ys1 = sorted(r.y1 for r in rects)
    m = len(rects)
    mid = m // 2
    if m % 2:
        x0, y0, x1, y1 = xs0[mid], ys0[mid], xs1[mid], ys1[mid]
    else:
        x0 = (xs0[mid - 1] + xs0[mid]) / 2
        y0 = (ys0[mid - 1] + ys0[mid]) / 2
        x1 = (xs1[mid - 1] + xs1[mid]) / 2
        y1 = (ys1[mid - 1] + ys1[mid]) / 2
    return fitz.Rect(x0, y0, x1, y1)


def union_rect(rects):
    out = rects[0]
    for r in rects[1:]:
        out |= r
    return out


# ---------------------------------------------------------------- 公共处理流程
# 下面三个函数是 tkinter 窗口与网页面板**共用**的唯一实现。
# 上一轮的教训：两条入口各自复制一份扫描逻辑，结果只给其中一条加了多进程，
# 另一条一直是串行。任何优化都必须加在这里，不要在入口里另写一份。

def parse_drop_paths(tkroot, data):
    """把 <<Drop>> 事件里的 event.data 解析成绝对路径列表。

    为什么必须用 tkroot.tk.splitlist()：
      Windows 下带空格的路径会被 Tcl 用花括号包裹，形如
        {C:/Users/a b/1#楼 图纸.pdf} C:/tmp/x.pdf
      用 str.split() 会把路径从空格处撕开（'.../1#楼' + '图纸.pdf}'），
      之后 os.path.isfile 全部为 False，表现出来就是"拖进去没反应"。
      splitlist 是 Tcl 自己的列表解析器，天生懂 {} 和 \" 转义。

    返回 (pdf路径列表, 被忽略的条目数)。
    """
    try:
        raw = tkroot.tk.splitlist(data)
    except Exception:
        raw = [data]

    paths, seen, ignored = [], set(), 0
    for item in raw:
        p = str(item).strip().strip('"').strip("'")
        if not p:
            continue
        if not p.lower().endswith(".pdf") or not os.path.isfile(p):
            ignored += 1
            continue
        ap = os.path.abspath(p)
        if ap not in seen:
            seen.add(ap)
            paths.append(ap)
    return paths, ignored


def unique_out(src, out_dir=None):
    """生成不与已有文件冲突的输出路径：xxx_已裁剪.pdf、xxx_已裁剪_2.pdf …

    为什么要这个：输出文件常常还开在 PDF 阅读器里，直接写会 PermissionError。
    """
    d = out_dir or os.path.dirname(os.path.abspath(src))
    stem = os.path.splitext(os.path.basename(src))[0] + "_已裁剪"
    p = os.path.join(d, stem + ".pdf")
    k = 2
    while os.path.exists(p):
        p = os.path.join(d, f"{stem}_{k}.pdf")
        k += 1
    return p


def process_one(src, out=None, margin=6.0, mode="each", only="", workers=0,
                progress=None, log=None):
    """处理一份 PDF：扫描每页内容框 → 写回 CropBox → 保存。**同步阻塞**。

    progress(frac, text)  frac 0~0.5 是扫描阶段，0.5~1.0 是写回阶段
    log(text)             人类可读的进度描述

    返回 dict：
      ok, skipped, src, out, pages, cropped, blanks, elapsed, error, rows
      rows = [(页码, "原W×H", "新W×H", 保留百分比), ...]
    """
    res = {"ok": False, "skipped": False, "src": src, "out": None,
           "pages": 0, "cropped": 0, "blanks": [], "elapsed": 0.0,
           "error": None, "rows": []}

    def _p(frac, text=""):
        if progress:
            try:
                progress(frac, text)
            except Exception:
                pass

    def _l(text):
        if log:
            try:
                log(text)
            except Exception:
                pass

    t0 = time.time()
    try:
        src = os.path.abspath(str(src).strip().strip('"'))
        if not os.path.isfile(src):
            res["error"] = f"找不到文件：{src}"
            return res

        try:
            margin = float(margin)
        except (TypeError, ValueError):
            res["error"] = "边距必须是数字"
            return res

        pad = margin * MM_TO_PT
        doc = fitz.open(src)
        n = doc.page_count
        doc.close()
        res["pages"] = n

        targets = parse_page_spec(only, n) if only else set(range(n))
        if only and not targets:
            res["error"] = f"「{only}」没匹配到任何页（有效范围 1-{n}）"
            return res

        ncpu = os.cpu_count() or 4
        eff = workers if workers and workers > 0 else min(ncpu, max(1, n))
        _l(f"共 {n} 页，{eff} 进程并行扫描中…（CPU {ncpu} 核）")

        content, blanks = scan_pages_parallel(
            src, n, workers,
            progress=lambda d, t: _p(0.5 * d / max(t, 1), f"扫描中 {d}/{t}"))
        res["blanks"] = blanks
        _l(f"扫描完成：检出内容 {len(content)} 页"
           + (f"，空白页 {len(blanks)} 页" if blanks else ""))

        if not content:
            res["skipped"] = True
            res["error"] = "没有任何页面检出内容（可能是全白页）"
            return res

        fixed = None
        if mode in ("uniform", "union"):
            rects = list(content.values())
            fixed = median_rect(rects) if mode == "uniform" else union_rect(rects)
            _l(f"统一裁剪框：{fixed.x0:.1f},{fixed.y0:.1f} → "
               f"{fixed.x1:.1f},{fixed.y1:.1f} pt")

        doc = fitz.open(src)
        cnt = 0
        for i in range(n):
            page = doc[i]
            pr = page.rect
            base = fixed if (fixed and i in content) else content.get(i)
            if base is None or i not in targets:
                continue
            cr = fitz.Rect(base.x0 - pad, base.y0 - pad,
                           base.x1 + pad, base.y1 + pad) & pr
            if cr.width < 1 or cr.height < 1:
                continue
            page.set_cropbox(cr)
            cnt += 1
            ratio = (cr.width * cr.height / (pr.width * pr.height) * 100) \
                if pr.width and pr.height else 0
            res["rows"].append((i + 1, f"{pr.width:.0f}×{pr.height:.0f}",
                                f"{cr.width:.0f}×{cr.height:.0f}", int(ratio)))
            _p(0.5 + 0.5 * (i + 1) / n, f"写回裁剪框 {i + 1}/{n}")

        res["cropped"] = cnt
        if cnt == 0:
            doc.close()
            res["skipped"] = True
            res["error"] = "没有页面被裁剪（选中的页可能都是空白）"
            return res

        if not out:
            out = unique_out(src)
        out = os.path.abspath(str(out).strip().strip('"'))
        if out == src:
            doc.close()
            res["error"] = "输出文件不能与源文件相同"
            return res

        # garbage=3 会做全对象清理，19MB 文件实测 >100 秒；
        # 只改 CropBox 不删对象，garbage=0 即可，体积几乎不变。
        try:
            doc.save(out, deflate=True, garbage=0)
        except PermissionError:
            # 输出文件还开在阅读器里 → 换个名字重试
            doc.close()
            doc = fitz.open(src)
            for i in range(n):
                page = doc[i]
                pr = page.rect
                base = fixed if (fixed and i in content) else content.get(i)
                if base is None or i not in targets:
                    continue
                cr = fitz.Rect(base.x0 - pad, base.y0 - pad,
                               base.x1 + pad, base.y1 + pad) & pr
                if cr.width >= 1 and cr.height >= 1:
                    page.set_cropbox(cr)
            alt = unique_out(src, os.path.dirname(out))
            _l(f"目标文件被占用，改存：{os.path.basename(alt)}")
            doc.save(alt, deflate=True, garbage=0)
            out = alt
        doc.close()

        res["out"] = out
        res["ok"] = True
        _l(f"完成：{cnt} 页已裁剪 → {os.path.basename(out)}")
        return res
    except Exception as e:  # noqa: BLE001
        import traceback
        res["error"] = f"{e}\n{traceback.format_exc()[-600:]}"
        return res
    finally:
        res["elapsed"] = time.time() - t0


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(
        description="PDF 每页独立智能裁剪（去白边）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("src", help="输入 PDF")
    ap.add_argument("-o", "--out", help="输出 PDF（默认 <输入名>_已裁剪.pdf）")
    ap.add_argument("-m", "--margin", type=float, default=6.0, help="内容外扩边距，默认 6")
    ap.add_argument("--unit", choices=["mm", "pt"], default="mm",
                    help="边距单位，默认 mm；pt = 1/72 英寸")
    ap.add_argument("--mode", choices=["each", "uniform", "union"], default="each",
                    help="each=每页各自裁（默认）；uniform=按各页中位数统一；union=按并集统一")
    ap.add_argument("--only", help="只裁这些页，如 1-20,35,40-50（页码 1 起）")
    ap.add_argument("--dry-run", action="store_true", help="只打印结果，不写文件")
    ap.add_argument("-j", "--workers", type=int, default=0,
                    help="并行进程数，0=自动（按 CPU 核数）；1=单线程")
    args = ap.parse_args()

    pad = args.margin if args.unit == "pt" else args.margin * MM_TO_PT

    doc = fitz.open(args.src)
    n = doc.page_count
    targets = parse_page_spec(args.only, n) if args.only else set(range(n))

    # --- 第一遍：算出每页内容框（多进程并行）---
    t0 = time.time()
    print(f"共 {n} 页，{args.workers or 'auto'} 进程并行扫描中…")
    sys.stdout.flush()

    def cli_prog(done, total):
        print(f"\r  扫描进度 {done}/{total}", end="", flush=True)

    content, blanks = scan_pages_parallel(args.src, n, args.workers,
                                          progress=cli_prog)
    print(f"\r  扫描完成，用时 {time.time() - t0:.1f}s{' ' * 20}")

    print(f"检出内容 {len(content)} 页")
    if blanks:
        print(f"空白页（跳过，不裁）：{', '.join(map(str, blanks))}")

    if not content:
        print("没有任何页面检出内容，退出。")
        return

    # --- 决定用哪种统一框 ---
    fixed_rect = None
    if args.mode in ("uniform", "union"):
        rects = list(content.values())
        fixed_rect = median_rect(rects) if args.mode == "uniform" else union_rect(rects)
        print(f"统一裁剪框（{args.mode}）："
              f"L={fixed_rect.x0:.1f} T={fixed_rect.y0:.1f} "
              f"R={fixed_rect.x1:.1f} B={fixed_rect.y1:.1f} pt")

    # --- 第二遍：写回 CropBox ---
    changed = 0
    print("\n页码    原始框(W×H pt)          裁剪框(W×H pt)          尺寸变化")
    print("-" * 74)
    for i in range(n):
        page = doc[i]
        pr = page.rect  # 页面可见框（MediaBox）
        base = fixed_rect if (fixed_rect and i in content) else content.get(i)

        if base is None or i not in targets:
            continue  # 空白页 / 未选中页：原样保留

        cr = fitz.Rect(base.x0 - pad, base.y0 - pad, base.x1 + pad, base.y1 + pad)
        # 夹到页面范围内，避免出现负坐标或超出页面
        cr &= pr
        # 防止被裁成 0 宽或高度为负
        if cr.width < 1 or cr.height < 1:
            continue

        old_wh = f"{pr.width:.0f}×{pr.height:.0f}"
        new_wh = f"{cr.width:.0f}×{cr.height:.0f}"
        ratio = cr.width * cr.height / (pr.width * pr.height) * 100 if pr.width * pr.height else 0
        print(f"{i+1:>4}    {old_wh:<22} {new_wh:<22} 保留 {ratio:.0f}%")

        if not args.dry_run:
            page.set_cropbox(cr)
        changed += 1

    if args.dry_run:
        print("\n[dry-run] 未写入文件。去掉 --dry-run 即可实际输出。")
        return

    out = args.out or (args.src.rsplit(".", 1)[0] + "_已裁剪.pdf")
    # garbage=3 会做全对象清理，19MB 文件实测 >100 秒；
    # 这里只改 CropBox 不删对象，用 garbage=0 即可，体积几乎不变。
    doc.save(out, deflate=True, garbage=0)
    doc.close()
    print(f"\n完成：{changed} 页已裁剪 → {out}")


# ---------------------------------------------------------------- 图形界面

def run_gui():
    """图形入口：优先 tkinter 窗口，没有 tkinter 则用浏览器面板。"""
    try:
        import tkinter  # noqa: F401
    except ImportError:
        # 这不是错误，只是当前 Python 没编译 tkinter，改用浏览器面板照常干活
        return run_web_gui()
    return run_tk_gui()


def run_web_gui():
    """无 tkinter 时的界面：起一个本地小服务，用浏览器当操作面板。
    只用标准库，任何Python 都能跑。网页点按钮直接出结果，不用手敲命令。"""
    import http.server
    import json
    import os
    import socketserver
    import threading
    import urllib.parse
    import webbrowser

    HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>PDF 批量智能裁剪</title>
<style>
 *{box-sizing:border-box}
 body{font-family:"Microsoft YaHei",sans-serif;background:#1e1e1e;color:#e8e8e8;
      padding:34px 40px;max-width:760px;margin:auto}
 h1{font-size:23px;color:#fff;margin:0 0 6px}
 .sub{color:#8a8a8a;font-size:13px;margin-bottom:22px}
 .box{background:#2d2d2d;padding:20px 22px;border-radius:8px;margin:16px 0}
 label{display:block;margin:13px 0 6px;color:#b9b9b9;font-size:14px}
 input,select{width:100%;padding:11px;background:#1a1a1a;color:#e8e8e8;
      border:1px solid #444;border-radius:5px;font-size:14px}
 input:focus,select:focus{outline:none;border-color:#0a7d3c}
 .hint{font-size:12px;color:#7a7a7a;margin-top:5px;line-height:1.7}
 button{padding:14px 32px;background:#0a7d3c;color:#fff;border:0;border-radius:6px;
      font-size:16px;cursor:pointer;font-family:inherit}
 button:hover{background:#0c8f45}
 button:disabled{background:#555;cursor:not-allowed}
 .sec{background:#2d2d2d;padding:20px 22px;border-radius:8px;margin:16px 0}
 .sec h2{font-size:15px;margin:0 0 12px;color:#fff}
 pre{background:#1a1a1a;padding:14px;border-radius:5px;font-size:12.5px;
     max-height:340px;overflow:auto;margin:0;color:#cfcfcf;
     font-family:Consolas,"Courier New",monospace;line-height:1.65;white-space:pre-wrap}
 .ok{color:#6ee06e;font-weight:bold} .err{color:#ff7b7b;font-weight:bold}
 .row2{display:flex;gap:14px}.row2>*{flex:1}
</style></head><body>

<h1>PDF 批量智能裁剪</h1>
<div class="sub">每页单独识别内容边界，一次处理整份文档 · 矢量裁剪，文字仍可搜索复制</div>

<div class="box">
  <label>PDF 文件路径</label>
  <input id="src" placeholder="把文件完整路径粘进来，或点下面按钮自动查找">
  <div class="row2" style="margin-top:10px">
    <button onclick="pick()" style="padding:9px;font-size:13px;background:#3a3a3a">自动查找最近文件…</button>
  </div>
  <div class="hint">不会填路径？点上面按钮，会自动列出最近改过的 PDF 供你选。</div>

  <div class="row2" style="margin-top:16px">
    <div>
      <label>保留边距（毫米）</label>
      <input id="margin" value="6">
      <div class="hint">建议 6~10。设 0 易切掉贴边的页码</div>
    </div>
    <div>
      <label>裁剪模式</label>
      <select id="mode">
        <option value="each">每页各自裁（推荐）</option>
        <option value="uniform">按中位数统一裁</option>
        <option value="union">按并集统一裁（最保守）</option>
      </select>
    </div>
  </div>

  <div class="row2" style="margin-top:16px">
    <div>
      <label>并行进程数</label>
      <select id="workers">
        <option value="0">自动（推荐）</option>
      </select>
      <div class="hint" id="cpuinfo">按 CPU 核数自动分配</div>
    </div>
    <div>
      <label>仅裁部分页（留空 = 全部）</label>
      <input id="only" placeholder="例：1-20,35,40-50">
      <div class="hint">空白页会自动跳过</div>
    </div>
  </div>

  <label>输出路径（留空 = 与源文件同目录，文件名加 _已裁剪）</label>
  <input id="out" placeholder="留空即可">
</div>

<button id="go" onclick="run()" style="width:100%">开始裁剪</button>

<div id="wrap" style="display:none;margin-top:16px">
  <div style="height:10px;background:#2d2d2d;border-radius:5px;overflow:hidden">
    <div id="bar" style="height:100%;width:0;background:#0a7d3c;
         transition:width .3s;border-radius:5px"></div>
  </div>
  <div class="hint" id="pct" style="text-align:center;margin-top:7px">准备中…</div>
</div>

<div class="sec" id="res" style="display:none">
  <h2>处理结果</h2>
  <pre id="log"></pre>
  <div class="hint" id="fin"></div>
</div>

<script>
fetch('/progress').then(r=>r.json());
fetch('/list').then(r=>r.json()).then(function(d){
  var sel=document.getElementById('workers');
  var info=document.getElementById('cpuinfo');
  var base=sel.options[0];
  // 允许1~16 核选择；"自动" 始终排第一
  for(var i=1;i<=16;i++){
    var o=document.createElement('option');
    o.value=i;o.textContent='并行 '+i+' 进程';
    sel.appendChild(o);
  }
});
function pick(){
  fetch('/list').then(r=>r.json()).then(d=>{
    if(!d.files.length){alert('没找到 PDF 文件。请把完整路径直接粘进上面的输入框。');return;}
    var s=document.getElementById('src');
    if(d.files.length===1){s.value=d.files[0];return;}
    var pick_str=prompt('找到这些 PDF，请输入序号选择（1-'+d.files.length+'）：\n\n'+
      d.files.map(function(f,i){return (i+1)+'. '+f;}).join('\n'),
      '1');
    if(pick_str){s.value=d.files[parseInt(pick_str)-1]||d.files[0];}
  });
}
var timer=null;
function run(){
  var src=document.getElementById('src').value.trim();
  if(!src){alert('请先填写 PDF 路径');return;}
  var btn=document.getElementById('go');btn.disabled=true;btn.textContent='处理中…';
  var box=document.getElementById('res');box.style.display='block';
  var log=document.getElementById('log');log.textContent='正在启动并行进程…';
  document.getElementById('wrap').style.display='block';

  // 轮询进度，避免长任务看不到动静
  clearInterval(timer);
  timer=setInterval(function(){
    fetch('/progress').then(r=>r.json()).then(function(d){
      var p=Math.round((d.progress||0)*100);
      document.getElementById('bar').style.width=p+'%';
      document.getElementById('pct').textContent=p>0&&p<100?('扫描中 '+p+'%'):'';
    }).catch(function(){});
  },400);

  fetch('/crop',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
      src:src,
      margin:document.getElementById('margin').value,
      mode:document.getElementById('mode').value,
      only:document.getElementById('only').value,
      workers:document.getElementById('workers').value,
      out:document.getElementById('out').value
    })})
  .then(r=>r.json())
  .then(function(d){
    clearInterval(timer);
    document.getElementById('bar').style.width='100%';
    document.getElementById('pct').textContent='';
    log.textContent=d.log;
    var fin=document.getElementById('fin');
    if(d.ok){
      fin.innerHTML='<span class="ok">✔ 完成</span>　输出文件：<br><b>'+d.out+'</b>'
        +'<br><div class="hint">已尝试用默认程序打开，若没弹出请手动到上述路径查看。</div>';
    }else{
      fin.innerHTML='<span class="err">✘ 失败</span>';
    }
    btn.disabled=false;btn.textContent='开始裁剪';
  })
  .catch(function(e){
    clearInterval(timer);
    log.textContent='请求失败：'+e;
    document.getElementById('fin').innerHTML='<span class="err">✘ 出错了</span>';
    btn.disabled=false;btn.textContent='开始裁剪';
  });
}
</script></body></html>"""

    state = {"last_dir": os.path.dirname(os.path.abspath(sys.argv[0])),
             "progress": 0.0}  # progress 由裁剪线程写、进度接口读

    def _open_file(path):
        """用系统默认程序打开文件（失败就忽略，不影响裁剪结果）。"""
        try:
            if sys.platform == "win32":
                os.startfile(path)  # noqa
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", path])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", path])
        except Exception:
            pass

    # ---------------------------------------------------------- 后端逻辑

    def find_recent_pdfs():
        """在常见位置找最近的 PDF，省得用户手敲长路径。"""
        import time

        roots = [
            os.path.expanduser("~"),
            os.path.join(os.path.expanduser("~"), "Desktop"),
            os.path.join(os.path.expanduser("~"), "Documents"),
            os.path.join(os.path.expanduser("~"), "Downloads"),
        ]
        seen = set()
        found = []
        deadline = time.time() + 3.5  # 硬性限时，绝不让网页卡住

        for r in roots:
            if time.time() > deadline:
                break
            if not os.path.isdir(r):
                continue
            # 只扫一层子目录：递归全盘（尤其网盘目录）会跑到几十秒甚至几分钟
            try:
                subs = [r] + [os.path.join(r, d) for d in os.listdir(r)[:200]
                               if os.path.isdir(os.path.join(r, d))]
            except OSError:
                continue
            for base in subs:
                if time.time() > deadline:
                    break
                try:
                    for name in os.listdir(base):
                        if not name.lower().endswith(".pdf"):
                            continue
                        f = os.path.join(base, name)
                        if f in seen or not os.path.isfile(f):
                            continue
                        seen.add(f)
                        found.append((os.path.getmtime(f), f))
                except OSError:
                    continue

        found.sort(key=lambda x: x[0], reverse=True)
        return [f for _, f in found[:12]]

    def do_crop(req):
        """执行裁剪，返回 (日志, 输出路径, 是否成功)。

        实际工作全部委托给 process_one()，与 tkinter 窗口共用同一份实现。
        这样任何性能优化（比如多进程）都不会再出现"只改了其中一个入口"的问题。
        """
        src_path = (req.get("src") or "").strip().strip('"')
        if not src_path:
            return "请填写 PDF 路径。\n", "", False
        try:
            margin = float(req.get("margin") or 6)
        except ValueError:
            return "边距请填数字。\n", "", False

        state["progress"] = 0.0

        def prog(frac, _text=""):
            state["progress"] = frac

        out = (req.get("out") or "").strip().strip('"')
        res = process_one(
            src_path, out=out or None, margin=margin,
            mode=req.get("mode", "each"),
            only=(req.get("only") or "").strip(),
            workers=int(req.get("workers") or 0),
            progress=prog)

        lines = [f"文件：{res['src']}", f"总页数：{res['pages']}    边距：{margin} 毫米"]
        ncpu = os.cpu_count() or 4
        lines.append(f"并行进程：{min(ncpu, max(1, res['pages']))}（CPU {ncpu} 核）")
        lines.append(f"扫描 + 写回用时 {res['elapsed']:.1f} 秒")

        if res["error"]:
            return "\n".join(lines) + f"\n\n{res['error']}\n", "", False

        if res["blanks"]:
            shown = ", ".join(map(str, res["blanks"][:25]))
            more = " …" if len(res["blanks"]) > 25 else ""
            lines.append(f"空白页跳过（{len(res['blanks'])} 页）：{shown}{more}")

        lines.append("")
        lines.append("页码   原始尺寸        裁剪后         保留")
        lines.append("-" * 52)
        for pno, old_wh, new_wh, ratio in res["rows"]:
            lines.append(f"{pno:>4}   {old_wh}".ljust(24) + f"{new_wh}".ljust(16)
                         + f"{ratio}%")
        lines.append("")
        lines.append(f"已裁剪 {res['cropped']} 页")
        return "\n".join(lines) + "\n", res["out"] or "", True

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass  # 静默，不要刷屏

        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            b = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, HTML)
            elif self.path == "/list":
                self._send(200, json.dumps({"files": find_recent_pdfs()},
                                           ensure_ascii=False),
                           "application/json; charset=utf-8")
            elif self.path == "/progress":
                self._send(200, json.dumps({"progress": state["progress"]},
                                           ensure_ascii=False),
                           "application/json; charset=utf-8")
            else:
                self._send(404, "not found")

        def do_POST(self):
            if self.path != "/crop":
                self._send(404, "not found")
                return
            try:
                ln = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(ln).decode("utf-8") or "{}")
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "log": f"请求解析失败：{e}"},
                                           ensure_ascii=False),
                           "application/json; charset=utf-8")
                return

            state["progress"] = 0.0
            log, out, ok = do_crop(req)
            if ok and out:
                # 打开文件可能弹窗阻塞，放后台线程，避免拖住 HTTP 响应
                threading.Thread(target=_open_file, args=(out,), daemon=True).start()
            self._send(200, json.dumps({"ok": ok, "log": log, "out": out,
                                        "opened": True}, ensure_ascii=False),
                       "application/json; charset=utf-8")

    class Server(socketserver.ThreadingTCPServer):
        """多线程：浏览器会并发请求（页面 + /list），单线程会互相阻塞卡死。"""
        allow_reuse_address = True
        daemon_threads = True

    httpd = Server(("127.0.0.1", 0), Handler)
    globals()["_HTTPD"] = httpd  # 暴露句柄，便于外部/测试访问
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/"

    print("=" * 58)
    print("  PDF 批量智能裁剪")
    print("=" * 58)
    print(f"操作面板已在浏览器打开：{url}")
    print("关掉这个黑色命令行窗口即退出程序。")
    print()

    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        webbrowser.open(url)
    except Exception:
        pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()

def run_tk_gui(_selftest_dir=None):
    """原生桌面窗口：拖入加入文件列表 → 点「开始裁剪」才真正执行。

    设计要点（改之前先读，否则很容易踩回去）：
    1. **两段式**：拖入只调用 add_files() 把行加进列表，状态「等待」；
       必须点「开始裁剪」触发 start_batch() 才把行送进 JOBS。
       以前的写法是 on_drop 直接 enqueue，文件一进队列 worker 立刻取走，
       用户既看不到清单，也拦不住。
    2. 拖拽区用**单个 Canvas**。tkdnd 是 per-window(OLE) 注册，子控件有独立
       HWND，父窗口注册不覆盖子控件；Frame 套 Label 就必须逐个注册，
       漏一个就"拖到文字上没反应"。所以「大区/窄条」两种形态复用同一个 Canvas。
    3. <<DropEnter>> / <<Drop>> 必须 return COPY，否则部分拖拽源显示
       "禁止放置"光标，甚至不投递。
    4. 处理在 worker 线程，主线程只用 root.after 轮询 queue。
       tkinter 不是线程安全的：worker 里绝不能碰 widget、不能调 root.after()、
       不能弹 messagebox，唯一通道是 queue.Queue.put()。
    5. 批次收尾按**本批快照** batch_total/batch_done 计数。不能用
       "finished >= seen"：中途拖入的新行不进本批，seen 会变大，批次永不结束。
    6. 扫描走 process_one()，与网页面板共用同一份实现。
    """
    import queue
    import threading
    import traceback
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    bg, fg, dim, entry_bg, accent = "#1e1e1e", "#e8e8e8", "#8a8a8a", "#2d2d2d", "#0a7d3c"
    zone_bg, zone_line, sep_color = "#232323", "#3d3d3d", "#2e2e2e"
    MAX_ROWS = 200          # 列表软上限，超出提示分批添加

    # 这些必须在任何会把控件画出来的代码之前定义：
    # relayout() 绑在 <Configure> 上，窗口一建就可能触发并读 items，
    # 定义晚了会 NameError（而且是 Tk 回调异常，只会在日志里出现）。
    JOBS = queue.Queue()   # 主线程 put (uid, src, opt)，worker get
    UIQ = queue.Queue()    # worker put，主线程 get（唯一线程安全通道）
    items = {}             # uid -> item，dict 保序即显示顺序
    by_key = {}            # normcase(abspath) -> item，用于去重
    TERMINAL = ("done", "fail", "skip")
    state = {"busy": False, "batch_total": 0, "batch_done": 0,
             "results": [], "last_dir": None, "next_uid": 0,
             "quiet": False}   # quiet=True 时抑制弹窗（自检用）

    # ---------------- 建窗口（拖拽扩展装不上就降级） ----------------
    has_dnd = False
    dnd_files = "DND_Files"
    copy_action = "copy"
    try:
        from tkinterdnd2 import TkinterDnD, DND_FILES
        try:
            from tkinterdnd2 import COPY as _COPY
            copy_action = _COPY
        except Exception:
            pass
        dnd_files = DND_FILES
        root = TkinterDnD.Tk()
        has_dnd = True
    except Exception:
        # TkinterDnD.Tk() 是"先建窗、后装扩展"：_require() 失败时窗口已经建好了。
        # 不销毁就再 tk.Tk()，会出现两个 root（后一个抢不到事件，界面诡异）。
        try:
            if getattr(tk, "_default_root", None) is not None:
                tk._default_root.destroy()
        except Exception:
            pass
        root = tk.Tk()

    root.title("PDF 智能裁剪")
    root.geometry("680x640")
    root.minsize(620, 540)
    root.resizable(True, True)
    root.configure(bg=bg)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(1, weight=1)      # 空列表时由拖拽区撑满
    root.rowconfigure(2, weight=0)

    # ttk 默认主题是 vista，进度条/滚动条颜色无法覆盖，深色界面会很突兀。
    # 切到 clam 才能着色；切了就得把滚动条一起配色，否则冒出一条浅灰。
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    try:
        style.configure("TProgressbar", troughcolor="#2a2a2a", background=accent,
                        bordercolor="#2a2a2a", lightcolor=accent, darkcolor=accent)
        style.configure("Row.Horizontal.TProgressbar", troughcolor="#2d2d2d",
                        background="#4fbf7f", bordercolor="#2d2d2d",
                        lightcolor="#4fbf7f", darkcolor="#4fbf7f")
        style.configure("Vertical.TScrollbar", troughcolor=bg, background="#3a3a3a",
                        bordercolor=bg, darkcolor="#3a3a3a", lightcolor="#3a3a3a",
                        arrowcolor="#9a9a9a", gripcount=0, relief="flat")
        style.map("Vertical.TScrollbar", background=[("active", "#4b4b4b")])
    except tk.TclError:
        pass

    # ---------------- 顶部标题 ----------------
    head = tk.Frame(root, bg=bg)
    head.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 6))
    tk.Label(head, text="PDF 智能裁剪", bg=bg, fg="#ffffff",
             font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
    tk.Label(head, text="拖入加入列表 · 每页独立识别边界 · 不重采样",
             bg=bg, fg=dim, font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(10, 0))

    # ---------------- 拖拽区（单个 Canvas，两种形态复用） ----------------
    zone_compact = [False]
    zone = tk.Canvas(root, bg=zone_bg, highlightthickness=0, height=200, cursor="hand2")
    zone.grid(row=1, column=0, sticky="nsew", padx=18, pady=(2, 10))

    rect_id = zone.create_rectangle(12, 12, 13, 13, outline=zone_line, width=2, dash=(6, 4))
    t_main = zone.create_text(0, 0, text="把 PDF 拖到这里", fill="#e8e8e8",
                              font=("Microsoft YaHei UI", 16, "bold"))
    t_sub = zone.create_text(0, 0, text="松开加入列表 · 点「开始裁剪」才开始", fill=dim,
                             font=("Microsoft YaHei UI", 9))
    t_tip = zone.create_text(0, 0, text="", fill="#6f6f6f",
                             font=("Microsoft YaHei UI", 9))

    def zone_title():
        return (f"已加入 {len(items)} 个文件 · 继续拖入可追加"
                if zone_compact[0] else "把 PDF 拖到这里")

    def relayout(_evt=None):
        w = max(zone.winfo_width(), 2)
        h = max(zone.winfo_height(), 2)
        zone.coords(rect_id, 10, 10, w - 10, h - 10)
        if zone_compact[0]:
            zone.itemconfigure(t_main, text=zone_title(),
                               font=("Microsoft YaHei UI", 10, "bold"))
            zone.itemconfigure(t_sub, text="松开即追加到列表 · 点「开始裁剪」才开始",
                               font=("Microsoft YaHei UI", 9))
            zone.itemconfigure(t_tip, state="hidden")
            zone.coords(t_main, w / 2, h / 2 - 9)
            zone.coords(t_sub, w / 2, h / 2 + 12)
        else:
            zone.itemconfigure(t_main, text="把 PDF 拖到这里",
                               font=("Microsoft YaHei UI", 16, "bold"))
            zone.itemconfigure(t_sub, text="松开加入列表 · 点「开始裁剪」才开始",
                               font=("Microsoft YaHei UI", 9))
            zone.itemconfigure(t_tip, state="normal")
            zone.coords(t_main, w / 2, h / 2 - 20)
            zone.coords(t_sub, w / 2, h / 2 + 8)
            zone.coords(t_tip, w / 2, h - 22)

    zone.bind("<Configure>", relayout)

    # ---------------- 文件列表（可滚动） ----------------
    list_wrap = tk.Frame(root, bg=bg)
    list_wrap.grid(row=2, column=0, sticky="nsew", padx=18, pady=(0, 8))
    list_wrap.columnconfigure(0, weight=1)
    list_wrap.rowconfigure(0, weight=1)
    list_wrap.grid_remove()

    # height=1 很关键：不让 canvas 把"所有行总高"算进窗口 requested size，
    # 否则展开高级面板时窗口会被撑到屏幕外。
    list_canvas = tk.Canvas(list_wrap, bg=bg, highlightthickness=0, height=180,
                            yscrollincrement=20, takefocus=0)
    list_canvas.grid(row=0, column=0, sticky="nsew")

    list_sb = ttk.Scrollbar(list_wrap, orient="vertical", command=list_canvas.yview)
    list_sb.grid(row=0, column=1, sticky="ns")
    list_canvas.configure(yscrollcommand=list_sb.set)

    list_inner = tk.Frame(list_canvas, bg=bg)
    win_id = list_canvas.create_window((0, 0), window=list_inner, anchor="nw")

    def sync_scrollregion(_evt=None):
        try:
            bbox = list_canvas.bbox("all")
            list_canvas.configure(scrollregion=bbox or (0, 0, 0, 0))
            if list_inner.winfo_reqheight() > list_canvas.winfo_height():
                list_sb.grid()
            else:
                list_sb.grid_remove()
                list_canvas.yview_moveto(0)
        except tk.TclError:
            pass

    def on_canvas_configure(e):
        # 内嵌 frame 的宽度必须跟随 canvas，否则行内 sticky="ew" 不起作用、横向溢出
        try:
            list_canvas.itemconfigure(win_id, width=e.width)
        except tk.TclError:
            pass
        sync_scrollregion()

    list_canvas.bind("<Configure>", on_canvas_configure)
    # 行增删导致高度变化时，只有这里会触发 → 必须在这里更新 scrollregion
    list_inner.bind("<Configure>", sync_scrollregion)

    def on_wheel(e):
        # 不用 <Enter>/<Leave> 动态绑：子控件是独立 window，指针滑到文件名上
        # 会触发 canvas 的 <Leave>，动态解绑会导致"滑到文字上就滚不动"。
        # 永久 bind_all + 命中测试最稳，也不会抢高级面板 Spinbox 的滚轮。
        try:
            if not list_wrap.winfo_ismapped():
                return None
            w = root.winfo_containing(e.x_root, e.y_root)
            inside = False
            while w is not None:
                if w is list_wrap:
                    inside = True
                    break
                w = getattr(w, "master", None)
            if not inside:
                return None
            d = e.delta
            steps = -int(d / 120) or (-1 if d > 0 else 1)
            list_canvas.yview_scroll(steps * 3, "units")
            return "break"
        except Exception:
            return None

    root.bind_all("<MouseWheel>", on_wheel, add="+")

    # ---------------- 进度与状态 ----------------
    foot = tk.Frame(root, bg=bg)
    foot.grid(row=3, column=0, sticky="ew", padx=18)
    foot.columnconfigure(0, weight=1)
    bar = ttk.Progressbar(foot, mode="determinate", maximum=100)
    bar.grid(row=0, column=0, columnspan=2, sticky="ew")
    msg = tk.Label(foot, text="就绪 · 把 PDF 拖进上面的框", bg=bg, fg=fg, anchor="w",
                   font=("Microsoft YaHei UI", 9))
    msg.grid(row=1, column=0, sticky="ew", pady=(7, 0))
    counter = tk.Label(foot, text="", bg=bg, fg=accent, anchor="e",
                       font=("Microsoft YaHei UI", 9, "bold"))
    counter.grid(row=1, column=1, sticky="e", pady=(7, 0))
    # width=1 + sticky="ew"：不加的话一个长路径会把整个窗口撑宽
    hint = tk.Label(foot, text="", bg=bg, fg="#6f6f6f", anchor="w", width=1,
                    font=("Microsoft YaHei UI", 8))
    hint.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))

    # ---------------- 按钮行 ----------------
    tools = tk.Frame(root, bg=bg)
    tools.grid(row=4, column=0, sticky="ew", padx=18, pady=(10, 6))

    def mkbtn(parent, text, cmd, primary=False):
        return tk.Button(parent, text=text, command=cmd,
                         bg=accent if primary else "#3a3a3a",
                         fg="#ffffff" if primary else fg,
                         activebackground="#0c8f45", activeforeground="#ffffff",
                         disabledforeground="#7f9c8c",
                         relief="flat", padx=14, pady=5, cursor="hand2",
                         font=("Microsoft YaHei UI", 10, "bold" if primary else "normal"))

    btn_start = mkbtn(tools, "开始裁剪", lambda: start_batch(), primary=True)
    btn_start.pack(side="left")
    btn_pick = mkbtn(tools, "选择文件…", lambda: pick_files())
    btn_pick.pack(side="left", padx=(8, 0))
    btn_clear = mkbtn(tools, "清空", lambda: clear_all())
    btn_clear.pack(side="left", padx=8)
    btn_adv = mkbtn(tools, "高级 ▾", lambda: toggle_adv())
    btn_adv.pack(side="left")
    btn_open = mkbtn(tools, "结果目录", lambda: open_last_dir())
    btn_open.pack(side="right")

    # ---------------- 高级参数（默认收起） ----------------
    adv = tk.Frame(root, bg="#242424")
    adv.grid(row=5, column=0, sticky="ew", padx=18, pady=(0, 14))
    adv.columnconfigure(1, weight=1)
    adv.grid_remove()

    margin_var = tk.StringVar(value="6")
    mode_var = tk.StringVar(value="each")
    only_var = tk.StringVar(value="")
    outdir_var = tk.StringVar(value="")
    workers_var = tk.StringVar(value="0")
    autoopen_var = tk.BooleanVar(value=True)

    advrow = [0]

    def advlabel(text):
        lb = tk.Label(adv, text=text, bg="#242424", fg="#b9b9b9",
                      font=("Microsoft YaHei UI", 9))
        lb.grid(row=advrow[0], column=0, sticky="w", padx=(12, 8), pady=5)
        return lb

    advlabel("保留边距")
    mrow = tk.Frame(adv, bg="#242424")
    mrow.grid(row=advrow[0], column=1, sticky="w")
    tk.Spinbox(mrow, textvariable=margin_var, from_=0, to=50, width=5,
               bg=entry_bg, fg=fg, buttonbackground="#3a3a3a", insertbackground=fg,
               relief="flat").pack(side="left")
    tk.Label(mrow, text="毫米（0~50）", bg="#242424", fg="#7a7a7a",
             font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(6, 0))
    advrow[0] += 1

    advlabel("裁剪模式")
    mrow2 = tk.Frame(adv, bg="#242424")
    mrow2.grid(row=advrow[0], column=1, sticky="w")
    for text, val in (("每页各自裁", "each"), ("中位数统一", "uniform"), ("并集统一", "union")):
        tk.Radiobutton(mrow2, text=text, variable=mode_var, value=val,
                       bg="#242424", fg=fg, selectcolor=entry_bg,
                       activebackground="#242424", activeforeground=fg,
                       relief="flat", font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(0, 10))
    advrow[0] += 1

    advlabel("仅裁部分页")
    tk.Entry(adv, textvariable=only_var, bg=entry_bg, fg=fg, insertbackground=fg,
             relief="flat").grid(row=advrow[0], column=1, sticky="ew", padx=(0, 12), pady=5)
    advrow[0] += 1

    advlabel("输出目录")
    orow = tk.Frame(adv, bg="#242424")
    orow.grid(row=advrow[0], column=1, sticky="ew", padx=(0, 12), pady=5)
    orow.columnconfigure(0, weight=1)
    tk.Entry(orow, textvariable=outdir_var, bg=entry_bg, fg=fg, insertbackground=fg,
             relief="flat").grid(row=0, column=0, sticky="ew")
    tk.Button(orow, text="选…", command=lambda: pick_outdir(), bg="#3a3a3a", fg=fg,
              relief="flat", width=4, cursor="hand2").grid(row=0, column=1, padx=(6, 0))
    advrow[0] += 1

    advlabel("并行进程")
    wrow = tk.Frame(adv, bg="#242424")
    wrow.grid(row=advrow[0], column=1, sticky="w")
    tk.Spinbox(wrow, textvariable=workers_var, from_=0, to=32, width=5,
               bg=entry_bg, fg=fg, buttonbackground="#3a3a3a", insertbackground=fg,
               relief="flat").pack(side="left")
    tk.Label(wrow, text="0=自动（按 CPU 核数）", bg="#242424", fg="#7a7a7a",
             font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(6, 0))
    advrow[0] += 1

    tk.Checkbutton(adv, text="完成后自动打开结果", variable=autoopen_var,
                   bg="#242424", fg=fg, selectcolor=entry_bg,
                   activebackground="#242424", activeforeground=fg,
                   relief="flat", font=("Microsoft YaHei UI", 9)).grid(
        row=advrow[0], column=1, sticky="w", padx=(0, 12), pady=(4, 10))

    def toggle_adv():
        w, h = root.winfo_width(), root.winfo_height()
        if adv.winfo_ismapped():
            adv.grid_remove()
            btn_adv.config(text="高级 ▾")
        else:
            adv.grid()
            btn_adv.config(text="高级 ▴")
        root.update_idletasks()
        # 不用 geometry("")：列表存在时会让窗口突然变大/变小，只增不减更稳
        root.geometry(f"{max(w, root.winfo_reqwidth())}x{max(h, root.winfo_reqheight())}")

    def pick_outdir():
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            outdir_var.set(d)

    def open_last_dir():
        d = state["last_dir"]
        if d and os.path.isdir(d):
            os.startfile(d)  # noqa: S606
        else:
            messagebox.showinfo("提示", "还没有处理过文件。")

    # ---------------- 队列、状态与数据模型 ----------------
    # （JOBS / UIQ / items / by_key / state 已在函数开头声明）

    def snapshot_opts():
        try:
            margin = float(margin_var.get())
        except ValueError:
            margin = 6.0
        try:
            workers = int(workers_var.get())
        except ValueError:
            workers = 0
        return {"margin": margin, "mode": mode_var.get(), "only": only_var.get().strip(),
                "outdir": outdir_var.get().strip(), "workers": workers}

    def short_text(s, n=14):
        s = str(s)
        return s if len(s) <= n else s[:n - 1] + "…"

    def short_name(name, n=50):
        return name if len(name) <= n else name[:n // 2 - 1] + "…" + name[-(n // 2 - 1):]

    def new_item(path):
        state["next_uid"] += 1
        uid = state["next_uid"]
        it = {"uid": uid, "path": path, "name": os.path.basename(path),
              "short": short_name(os.path.basename(path)),
              "status": "wait", "frac": 0.0, "text": "", "result": None,
              "frame": None, "name_lbl": None, "bar": None,
              "status_lbl": None, "del_btn": None}
        items[uid] = it
        by_key[os.path.normcase(path)] = it
        return it

    def paint(it):
        s = it["status"]
        if s == "wait":
            txt, color = "等待", dim
        elif s == "queued":
            txt, color = "排队中", "#b9a26a"
        elif s == "run":
            txt, color = (it["text"] or "处理中…"), "#d8b45a"
        elif s == "done":
            r = it["result"] or {}
            txt, color = f"完成 {r.get('cropped', 0)} 页", "#4fbf7f"
        elif s == "skip":
            txt, color = "已跳过", dim
        else:
            txt, color = "失败", "#d97a6c"
        try:
            it["status_lbl"].config(text=short_text(txt, 14), fg=color)
            it["bar"]["value"] = max(0.0, min(100.0, it["frac"] * 100.0))
        except tk.TclError:
            pass

    def make_row(it):
        f = tk.Frame(list_inner, bg=bg)
        f.pack(fill="x", side="top")
        f.grid_columnconfigure(0, weight=1)

        # width=1 + sticky="ew"：让文件名靠列权重撑开并被裁剪，而不是把窗口撑宽。
        # 不要用 wraplength：行高会依赖宽度，宽度又依赖滚动条显隐，会抖动。
        name = tk.Label(f, text=it["short"], bg=bg, fg=fg, anchor="w", width=1,
                        font=("Microsoft YaHei UI", 9))
        name.grid(row=0, column=0, sticky="ew", padx=(2, 10), pady=(7, 2))

        pb = ttk.Progressbar(f, style="Row.Horizontal.TProgressbar",
                             mode="determinate", maximum=100, length=110)
        pb.grid(row=0, column=1, sticky="e", padx=(0, 10), pady=(7, 2))

        st = tk.Label(f, text="等待", bg=bg, fg=dim, anchor="e", width=14,
                      font=("Microsoft YaHei UI", 9))
        st.grid(row=0, column=2, sticky="e", pady=(7, 2))

        xb = tk.Button(f, text="×", command=lambda u=it["uid"]: remove_item(u),
                       bg=bg, fg="#8a8a8a", activebackground="#3a3a3a",
                       activeforeground="#ffffff", disabledforeground="#454545",
                       relief="flat", bd=0, width=2, cursor="hand2",
                       font=("Microsoft YaHei UI", 10))
        xb.grid(row=0, column=3, sticky="e", padx=(2, 4))

        # 分隔线放在行内部：删行时 frame.destroy() 一次带走，不会留下孤儿线
        sep = tk.Frame(f, bg=sep_color, height=1)
        sep.grid(row=1, column=0, columnspan=4, sticky="ew")

        it.update(frame=f, name_lbl=name, bar=pb, status_lbl=st, del_btn=xb)

        for w in (f, name, st, pb):
            w.bind("<Motion>", lambda e, i=it: show_path(i))
            w.bind("<Double-Button-1>",
                   lambda e, i=it: messagebox.showinfo("文件完整路径", i["path"]))
        sync_scrollregion()

    def show_path(it):
        hint.config(text=it["path"])

    def clear_hint():
        hint.config(text="")

    # ---------------- 列表模式切换 ----------------
    def enter_list_mode():
        if zone_compact[0]:
            return
        zone_compact[0] = True
        zone.config(height=72)
        zone.grid_configure(sticky="ew")     # sticky 必须跟着改，否则 height 无效
        root.rowconfigure(1, weight=0)
        root.rowconfigure(2, weight=1, minsize=200)
        list_wrap.grid()
        relayout()

    def exit_list_mode():
        if not zone_compact[0]:
            return
        zone_compact[0] = False
        zone.config(height=200)
        zone.grid_configure(sticky="nsew")
        root.rowconfigure(1, weight=1)
        root.rowconfigure(2, weight=0)
        list_wrap.grid_remove()
        clear_hint()
        relayout()

    # ---------------- 汇总与按钮状态 ----------------
    def refresh_totals():
        total = len(items)
        finished = waiting = fails = 0
        score = 0.0
        for it in items.values():
            s = it["status"]
            if s in TERMINAL:
                finished += 1
                score += 1.0          # 失败也算终态，否则总条永远停在 90%
            elif s in ("run", "queued"):
                score += it["frac"]
            else:
                waiting += 1
            if s == "fail":
                fails += 1
        bar["value"] = (score / total * 100.0) if total else 0.0

        if not total:
            counter.config(text="")
            return
        parts = [f"{finished}/{total} 完成"]
        if waiting:
            parts.append(f"{waiting} 等待")
        if fails:
            parts.append(f"{fails} 失败")
        counter.config(text=" · ".join(parts))

    def refresh_buttons():
        busy = state["busy"]
        waiting = sum(1 for i in items.values() if i["status"] == "wait")

        if busy:
            start_txt, start_on = "裁剪中…", False
        elif waiting:
            start_txt, start_on = f"开始裁剪 ({waiting})", True
        else:
            start_txt, start_on = "开始裁剪", False
        btn_start.config(text=start_txt, state="normal" if start_on else "disabled",
                         bg=accent if start_on else "#2c4a3a",
                         fg="#ffffff" if start_on else "#7f9c8c",
                         cursor="hand2" if start_on else "arrow")

        clear_on = bool(items) and not busy
        btn_clear.config(state="normal" if clear_on else "disabled",
                         fg=fg if clear_on else "#6f6f6f",
                         cursor="hand2" if clear_on else "arrow")

        # 处理中整列禁用 ×：删行会让批次计数与列表错位，收益为零
        for it in items.values():
            try:
                it["del_btn"].config(state="disabled" if busy else "normal",
                                     fg="#454545" if busy else "#8a8a8a",
                                     cursor="arrow" if busy else "hand2")
            except tk.TclError:
                pass

    # ---------------- 加入列表 / 开始 / 删除 ----------------
    def add_files(paths):
        paths = [p for p in paths if p]
        if not paths:
            return
        added = ignored = 0
        for p in paths:
            ap = os.path.abspath(p)
            key = os.path.normcase(ap)
            old = by_key.get(key)
            if old is not None:
                # 失败/跳过/已完成的行，重新拖入 = 原地复位，不用先删再拖
                if old["status"] in ("fail", "skip", "done"):
                    old.update(status="wait", frac=0.0, text="", result=None)
                    paint(old)
                    added += 1
                else:
                    ignored += 1
                continue
            if len(items) >= MAX_ROWS:
                ignored += 1
                continue
            it = new_item(ap)
            make_row(it)
            added += 1

        # 同名不同目录时，用「父目录名/文件名」区分
        counts = {}
        for it in items.values():
            counts[it["name"]] = counts.get(it["name"], 0) + 1
        for it in items.values():
            if counts[it["name"]] > 1:
                parent = os.path.basename(os.path.dirname(it["path"]))
                it["short"] = short_name(f"{parent}/{it['name']}")
            else:
                it["short"] = short_name(it["name"])
            try:
                it["name_lbl"].config(text=it["short"])
            except tk.TclError:
                pass

        if items:
            enter_list_mode()
        root.after_idle(sync_scrollregion)
        refresh_totals()
        refresh_buttons()

        bits = []
        if added:
            bits.append(f"已加入 {added} 个文件")
        if ignored:
            bits.append(f"忽略 {ignored} 个重复/超限")
        if not state["busy"] and any(i["status"] == "wait" for i in items.values()):
            bits.append("点「开始裁剪」开始")
        msg.config(text=" · ".join(bits) or "已加入")

    def start_batch():
        if state["busy"]:
            return
        todo = [i for i in items.values() if i["status"] == "wait"]
        if not todo:
            return
        opt = snapshot_opts()               # 本批冻结高级参数，中途改不影响本批
        state.update(busy=True, batch_total=len(todo), batch_done=0, results=[])
        for it in todo:
            it["status"], it["frac"] = "queued", 0.0
            paint(it)
        for it in todo:
            JOBS.put((it["uid"], it["path"], opt))   # 必须带 uid，否则进度无法路由到行
        msg.config(text=f"开始裁剪 {len(todo)} 个文件…")
        refresh_totals()
        refresh_buttons()

    def remove_item(uid):
        if state["busy"]:
            return
        it = items.pop(uid, None)
        if it is None:
            return
        by_key.pop(os.path.normcase(it["path"]), None)
        try:
            it["frame"].destroy()
        except tk.TclError:
            pass
        if not items:
            exit_list_mode()
        root.after_idle(sync_scrollregion)
        refresh_totals()
        refresh_buttons()
        msg.config(text=f"已移除 {it['name']}")

    def clear_all():
        if state["busy"]:
            return
        for it in list(items.values()):
            try:
                it["frame"].destroy()
            except tk.TclError:
                pass
        items.clear()
        by_key.clear()
        exit_list_mode()
        root.after_idle(sync_scrollregion)
        refresh_totals()
        refresh_buttons()
        msg.config(text="列表已清空 · 把 PDF 拖进上面的框")

    # ---------------- 后台线程 ----------------
    def worker_loop():
        """后台线程：只碰 queue 和 process_one，绝不碰 widget。"""
        while True:
            job = JOBS.get()
            if job is None:
                return
            uid, src, opt = job
            UIQ.put(("start", uid))
            last = [0.0]

            def prog(frac, text, _uid=uid):
                now = time.time()
                if now - last[0] < 0.08 and frac < 0.999:   # 节流，避免刷爆队列
                    return
                last[0] = now
                UIQ.put(("prog", _uid, frac, text))

            out = unique_out(src, opt["outdir"] or None)
            res = process_one(src, out=out, margin=opt["margin"], mode=opt["mode"],
                              only=opt["only"], workers=opt["workers"],
                              progress=prog, log=lambda s: UIQ.put(("log", s)))
            # 输出目录不可写时退回源目录
            if not res.get("ok") and opt["outdir"] and not res.get("skipped"):
                res = process_one(src, out=unique_out(src), margin=opt["margin"],
                                  mode=opt["mode"], only=opt["only"],
                                  workers=opt["workers"],
                                  progress=prog, log=lambda s: UIQ.put(("log", s)))
            UIQ.put(("done", uid, res))

    threading.Thread(target=worker_loop, daemon=True).start()

    def open_results(ok):
        try:
            dirs = {os.path.dirname(r["out"]) for r in ok}
            state["last_dir"] = ok[-1]["out"].rsplit(os.sep, 1)[0]
            if len(ok) == 1:
                os.startfile(ok[0]["out"])  # noqa: S606
            elif len(dirs) == 1:
                # 多文件同目录：开资源管理器，不要逐个开 PDF（会弹一堆阅读器）
                os.startfile(dirs.pop())  # noqa: S606
            else:
                import subprocess
                subprocess.Popen(f'explorer /select,"{ok[-1]["out"]}"')
        except Exception:
            pass

    def finish_batch():
        state["busy"] = False
        results = state["results"]
        ok = [r for r in results if r.get("ok")]
        skipped = [r for r in results if r.get("skipped")]
        # skipped 也带 error 文本，不能算失败（以前的判定会误报）
        bad = [r for r in results if not r.get("ok") and not r.get("skipped")]
        if ok:
            state["last_dir"] = ok[-1]["out"].rsplit(os.sep, 1)[0]
        waiting = sum(1 for i in items.values() if i["status"] == "wait")

        parts = [f"本轮完成 {len(ok)}/{len(results)}"]
        if bad:
            parts.append(f"失败 {len(bad)}")
        if skipped:
            parts.append(f"跳过 {len(skipped)}")
        if waiting:
            parts.append(f"还有 {waiting} 个等待，点「开始裁剪」继续")
        msg.config(text=" · ".join(parts))

        refresh_totals()
        refresh_buttons()

        if waiting:
            return      # 还有活没干，先别弹资源管理器

        if autoopen_var.get() and ok:
            open_results(ok)
        if bad and not state.get("quiet"):
            lines = []
            for r in bad[:5]:
                err = (r.get("error") or "未知原因").splitlines()[0]
                lines.append(f"{os.path.basename(r['src'])}：{err}")
            messagebox.showwarning("部分文件未处理", "\n".join(lines))

    def handle(item):
        kind = item[0]
        if kind == "start":
            it = items.get(item[1])
            if it is None:
                return
            it["status"], it["frac"], it["text"] = "run", 0.0, ""
            paint(it)
            refresh_totals()
            refresh_buttons()
            msg.config(text=f"处理中：{it['name']}")

        elif kind == "prog":
            it = items.get(item[1])
            if it is None:
                return
            # 用 max 防倒退：输出目录重试时 process_one 会从 0 重新推进度
            it["frac"] = max(it["frac"], min(1.0, item[2]))
            if item[3]:
                it["text"] = item[3]
            paint(it)
            refresh_totals()
            if item[3]:
                msg.config(text=f"{it['name']} · {item[3]}")

        elif kind == "log":
            msg.config(text=item[1])

        elif kind == "done":
            uid, res = item[1], item[2]
            # 先计数再查行：行若已不在（理论上处理中被禁止删除），
            # 也必须让批次走到收尾，否则 busy 永久为真、按钮卡死
            state["batch_done"] += 1
            it = items.get(uid)
            if it is not None:
                it["result"] = res
                it["frac"] = 1.0
                it["status"] = ("done" if res.get("ok")
                                else "skip" if res.get("skipped") else "fail")
                paint(it)
            state["results"].append(res)
            refresh_totals()
            refresh_buttons()
            if state["batch_done"] >= max(1, state["batch_total"]):
                finish_batch()

    def poll():
        try:
            for _ in range(400):          # 每轮限量，防止饿死界面
                handle(UIQ.get_nowait())
        except queue.Empty:
            pass
        except Exception:
            # 静默吞异常会让排查无从下手，至少落一次盘
            try:
                import tempfile
                with open(os.path.join(tempfile.gettempdir(), "pdfcrop_error.log"),
                          "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%F %T')} poll: {traceback.format_exc()}\n")
            except Exception:
                pass
        try:
            root.after(80, poll)
        except Exception:
            pass

    # ---------------- 拖拽事件 ----------------
    def on_enter(_evt):
        zone.itemconfigure(rect_id, outline=accent, fill="#1f3328")
        zone.itemconfigure(t_sub, text=("松开鼠标，追加到列表" if zone_compact[0]
                                        else "松开鼠标，加入列表（点开始才裁剪）"))
        return copy_action          # 必须返回动作，否则有些源不投递

    def on_leave(_evt):
        zone.itemconfigure(rect_id, outline=zone_line, fill=zone_bg)
        zone.itemconfigure(t_main, text=zone_title())
        zone.itemconfigure(t_sub, text=("松开即追加到列表 · 点「开始裁剪」才开始"
                                        if zone_compact[0]
                                        else "松开加入列表 · 点「开始裁剪」才开始"))

    def on_drop(evt):
        on_leave(None)
        try:
            files, ignored = parse_drop_paths(root, evt.data)
        except Exception as e:  # noqa: BLE001
            msg.config(text=f"解析拖入内容失败：{e}")
            return copy_action
        if not files:
            msg.config(text="没识别到 PDF" + (f"（忽略了 {ignored} 项）" if ignored else ""))
            return copy_action
        add_files(files)     # ← 只入列表，不启动
        return copy_action

    if has_dnd:
        for w in (zone, root):
            try:
                w.drop_target_register(dnd_files)
                w.dnd_bind("<<DropEnter>>", on_enter)
                w.dnd_bind("<<DropLeave>>", on_leave)
                w.dnd_bind("<<Drop>>", on_drop)
            except Exception:
                has_dnd = False

    zone.itemconfigure(t_tip, text=("或点击此区域选择文件" if has_dnd
                                    else "拖拽不可用，请点下方「选择文件」"))

    def zone_click(_evt):
        pick_files()

    zone.bind("<Button-1>", zone_click)

    def pick_files():
        ps = filedialog.askopenfilenames(
            title="选择要裁剪的 PDF",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")])
        if ps:
            add_files([os.path.abspath(p) for p in ps])

    # ---------------- 关闭 & 异常 ----------------
    def on_close():
        waiting = sum(1 for i in items.values() if i["status"] == "wait")
        if state["busy"]:
            if not messagebox.askyesno("正在处理", "还有文件没处理完，确定退出？"):
                return
            JOBS.put(None)
            os._exit(0)      # 不等子进程收尾，否则要卡 1~2 秒
        if waiting:
            if not messagebox.askyesno("还有文件未处理",
                                      f"列表里还有 {waiting} 个文件没开始，确定退出？"):
                return
        JOBS.put(None)
        try:
            root.destroy()
        except Exception:
            pass

    root.protocol("WM_DELETE_WINDOW", on_close)

    # --windowed 打包后 sys.stderr 是 None，Tk 回调异常会被静默吞掉，
    # 用户只看到"点了没反应"。必须落盘。
    def on_tk_error(exc, val, tb):
        try:
            msg.config(text=f"内部错误：{val}")
        except Exception:
            pass
        try:
            import tempfile
            p = os.path.join(tempfile.gettempdir(), "pdfcrop_error.log")
            with open(p, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%F %T')} {val}\n"
                        + "".join(traceback.format_exception(exc, val, tb)) + "\n")
        except Exception:
            pass

    root.report_callback_exception = on_tk_error

    # ---------------- 列表流程自检（--gui-selftest-list） ----------------
    def _run_list_selftest(out_dir):
        """自动验证「只加不跑 → 点开始 → 全部完成」整条链路。

        拖拽动作本身没法自动化，但队列/批次计数/uid 路由可以 ——
        而这里正是最容易死锁的地方（批次不收尾 → busy 永久为真 → 按钮卡死）。
        结果写 %TEMP%\\pdfcrop_ui_selftest.json，用 os._exit 带回退出码。
        """
        import json
        import tempfile

        report = os.path.join(tempfile.gettempdir(), "pdfcrop_ui_selftest.json")
        rep = {"ok": False, "steps": [], "generated": []}
        try:
            d = out_dir or tempfile.mkdtemp(prefix="pdfcrop_ui_")
            os.makedirs(d, exist_ok=True)
            n_files = max(1, int(os.environ.get("PDFCROP_UI_SELFTEST_N", "3") or 3))
            paths = []
            for i in range(n_files):
                p = os.path.join(d, f"ui test {i + 1}.pdf")   # 故意带空格
                doc = fitz.open()
                for _ in range(2):
                    pg = doc.new_page(width=595, height=842)
                    pg.draw_rect(fitz.Rect(60, 80, 535, 760),
                                 color=(0, 0, 0), fill=(0.9, 0.9, 0.9))
                doc.save(p, deflate=True, garbage=0)
                doc.close()
                paths.append(p)
            rep["generated"] = [os.path.basename(x) for x in paths]

            state["quiet"] = True
            autoopen_var.set(False)

            # 阶段一：只加进列表，绝不能自己跑起来
            add_files(paths)
            stage1 = (len(items) == n_files
                      and str(btn_start["state"]) == "normal"
                      and str(n_files) in btn_start["text"]
                      and zone_compact[0]
                      and not state["busy"])
            rep["items_after_add"] = len(items)
            rep["start_text"] = btn_start["text"]
            rep["steps"].append(f"add_files 只加不跑: {stage1}")

            # 列表结构断言：拖拽区已收窄、列表可见、canvas 已映射、行宽度跟随
            root.update()
            first = next(iter(items.values()), None)
            rep["layout"] = {
                "zone_compact": [zone.winfo_height(), str(zone.grid_info().get("sticky"))],
                "list_mapped": bool(list_wrap.winfo_ismapped()),
                "canvas_mapped": bool(list_canvas.winfo_ismapped()),
                "row_w": (first["frame"].winfo_width() if first else 0),
                "canvas_w": list_canvas.winfo_width(),
            }
            lay = rep["layout"]
            stage1 = (stage1
                      and lay["zone_compact"][0] <= 90
                      and lay["list_mapped"] and lay["canvas_mapped"]
                      and lay["row_w"] >= lay["canvas_w"] - 8)

            # 阶段二：点开始，等全部终态
            # 延时只为截图留出"已列出但未开始"的稳定画面（非阻塞，不冻界面）
            delay_ms = int(float(os.environ.get("PDFCROP_UI_SELFTEST_DELAY", "0") or 0) * 1000)
            deadline = time.time() + 90

            def wait_done():
                if state["busy"] and time.time() < deadline:
                    root.after(120, wait_done)
                    return
                sts = [it["status"] for it in items.values()]
                rep["statuses"] = sts
                rep["counter"] = counter["text"]
                # 直接断言"看得见的东西"：每行进度条数值 + 状态文字
                rep["visual"] = [{"bar": round(float(it["bar"]["value"]), 1),
                                  "status": it["status_lbl"]["text"]}
                                 for it in items.values()]
                rep["total_bar"] = round(float(bar["value"]), 1)
                stage2 = ((not state["busy"]) and bool(sts)
                          and all(s == "done" for s in sts)
                          and all(v["bar"] >= 99.9 for v in rep["visual"])
                          and rep["total_bar"] >= 99.9)
                rep["steps"].append(f"start_batch 全部完成且进度条满格: {stage2}")
                rep["ok"] = bool(stage1 and stage2)
                try:
                    with open(report, "w", encoding="utf-8") as f:
                        json.dump(rep, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass

                def _bye():          # 留 4 秒给截图看最终态
                    try:
                        root.destroy()
                    except Exception:
                        pass
                    os._exit(0 if rep["ok"] else 1)

                root.after(4000, _bye)

            def go():
                start_batch()
                rep["busy_after_start"] = state["busy"]
                root.after(200, wait_done)

            root.after(max(1, delay_ms), go)
        except Exception:
            rep["error"] = traceback.format_exc()[-800:]
            try:
                with open(report, "w", encoding="utf-8") as f:
                    json.dump(rep, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
            os._exit(1)

    relayout()
    refresh_buttons()
    refresh_totals()
    if _selftest_dir is not None:
        root.after(300, lambda: _run_list_selftest(_selftest_dir))
    root.after(80, poll)
    root.mainloop()


# ---------------------------------------------------------------- 自检（打包验证用）

def gui_selftest():
    """窗口能力自检：--gui-selftest[=报告路径]

    拖拽动作本身没法自动化测试，但「路径解析」和「tkdnd 是否真的装载成功」
    可以。打包后跑这一条，就能确认 exe 里拖拽是可用的，不必人工拖。

    退出码：0 全部通过 / 2 缺 tkinter 或 tkinterdnd2 / 3 tkdnd 装载失败 / 4 解析失败
    注意：--windowed 打包后 sys.stdout 是 None，**绝不能用 print()**，
    否则 flush() 会抛 AttributeError。结果只写文件 + 退出码。
    """
    import json
    import tempfile
    import traceback

    report_path = os.path.join(tempfile.gettempdir(), "pdfcrop_gui_selftest.json")
    for a in sys.argv[1:]:
        if a.startswith("--gui-selftest="):
            report_path = a.split("=", 1)[1].strip('"')

    rep = {"ok": False, "code": 1,
           "frozen": bool(getattr(sys, "frozen", False)),
           "executable": sys.executable,
           "meipass": getattr(sys, "_MEIPASS", None),
           "python": sys.version.split()[0],
           "report": report_path, "steps": []}

    root = None
    try:
        import tkinter as tk
        rep["tk"] = f"Tk {tk.TkVersion} / Tcl {tk.TclVersion}"
        rep["steps"].append("import tkinter: ok")

        from tkinterdnd2 import TkinterDnD, DND_FILES
        rep["steps"].append("import tkinterdnd2: ok")

        root = TkinterDnD.Tk()
        root.withdraw()
        rep["tkdnd_present"] = root.tk.call("package", "present", "tkdnd")
        rep["tkdnd_commands"] = str(root.tk.call("info", "commands", "tkdnd::drop_target"))
        rep["steps"].append(f"package present tkdnd: {rep['tkdnd_present'] or '空'}")

        zone = tk.Frame(root)
        zone.pack()
        zone.drop_target_register(DND_FILES)
        zone.dnd_bind("<<Drop>>", lambda e: "copy")
        rep["steps"].append("drop_target_register: ok")

        # 带空格路径必须解析正确（Windows 下会被 {} 包裹）
        tmp = os.path.join(tempfile.gettempdir(), "drop test 1.pdf")
        with open(tmp, "wb") as f:
            f.write(b"%PDF-1.4\n")
        data = "{" + tmp + "} " + tmp.replace(" ", "_")   # 第二个故意不存在
        paths, ignored = parse_drop_paths(root, data)
        rep["parsed"] = paths
        rep["ignored"] = ignored
        parse_ok = (len(paths) == 1 and paths[0] == os.path.abspath(tmp) and ignored == 1)
        rep["steps"].append(f"parse_drop_paths 带空格: {'ok' if parse_ok else 'FAIL'}")
        try:
            os.remove(tmp)
        except OSError:
            pass

        dnd_ok = bool(rep["tkdnd_present"]) and "tkdnd::drop_target" in rep["tkdnd_commands"]
        rep["ok"] = dnd_ok and parse_ok
        rep["code"] = 0 if rep["ok"] else (4 if dnd_ok else 3)
    except ImportError as e:
        rep["error"] = f"缺模块：{e}"
        rep["code"] = 2
    except Exception as e:  # noqa: BLE001
        rep["error"] = f"{e}\n{traceback.format_exc()[-800:]}"
        rep["code"] = 3
    finally:
        try:
            if root is not None:
                root.destroy()
        except Exception:
            pass

    try:
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return rep["code"]


# ---------------------------------------------------------------- 程序入口
# 注意：必须放在 run_gui 定义之后，否则会报 NameError

if __name__ == "__main__":
    # 打包成 exe 后，多进程子进程会重新导入主模块，
    # 必须先调 freeze_support()，否则子进程可能重复启动整个程序。
    # 顺序不能动：它必须比自检分支和任何进程/线程更早执行，
    # 否则 --multiprocessing-fork 子进程会被自检分支截走。
    try:
        import multiprocessing
        multiprocessing.freeze_support()
    except Exception:
        pass

    # 注意顺序：--gui-selftest-list 也以 --gui-selftest 开头，必须放前面先判
    if any(a.startswith("--gui-selftest-list") for a in sys.argv[1:]):
        d = ""
        for a in sys.argv[1:]:
            if a.startswith("--gui-selftest-list="):
                d = a.split("=", 1)[1].strip('"')
        run_tk_gui(d)          # 内部 os._exit 带回退出码
        sys.exit(0)
    if any(a.startswith("--gui-selftest") for a in sys.argv[1:]):
        sys.exit(gui_selftest())
    if "--web-gui" in sys.argv[1:]:
        run_web_gui()
        sys.exit(0)

    # 带参数 = 命令行模式；不带参数 = 图形界面（打包成 exe 后双击即用）
    if len(sys.argv) > 1:
        main()
    else:
        run_gui()
