#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PDF 页面内容边界识别与安全裁剪核心模块（兼做多进程 Worker）。

为什么把核心扫描与坐标转换全部集中在此模块？
1. Windows 的 multiprocessing 采用 spawn 机制，子进程会重新导入目标模块。
   将纯计算逻辑独立在此文件中（不含任何 GUI / CLI 顶层副作用），子进程导入轻量且安全。
2. 消除此前主文件与 worker 文件各抄一份扫描算法导致的逻辑分裂（DRY 原则），
   确保单核串行、多核并行、CLI、桌面窗口、Web 面板 100% 走同一套实现。
"""

import atexit
import math

try:
    import pymupdf as fitz
except ImportError:
    import fitz


DARK_THRESHOLD = 245          # 灰度 < 245 视为“有墨迹内容”（0=纯黑，255=纯白）
WHITE_NORM = DARK_THRESHOLD / 255.0   # 约 0.9608，与像素阈值严格对齐
PAGE_COVER_RATIO = 0.92       # 覆盖率阈值
IMG_CHECK_RATIO = 0.12        # 当外边界由较大图片（占页面 >=12%）决定时，用像素法剥离图片内白边

# 子进程持久化文档句柄，避免 500 页大文件被重复 fitz.open() 500 次
_WORKER_STATE = {"doc": None, "src": None, "min_pt": 1.0, "dpi": 72}


def _close_worker_doc():
    doc = _WORKER_STATE.get("doc")
    if doc is not None:
        try:
            doc.close()
        except Exception:
            pass
        _WORKER_STATE["doc"] = None
        _WORKER_STATE["src"] = None


atexit.register(_close_worker_doc)


# ---------------------------------------------------------------- 颜色与图元可见性

def _is_visible_color(color, opacity=1.0):
    """判断颜色在白纸背景上是否肉眼可见（过滤纯白背景块与全透明图元）。"""
    if color is None:
        return False
    if opacity is not None and opacity <= 0.01:
        return False
    if isinstance(color, (int, float)):
        return float(color) < WHITE_NORM
    n = len(color)
    if n == 0:
        return False
    if n == 1:
        return float(color[0]) < WHITE_NORM
    if n == 3:
        # PyMuPDF get_drawings() 已将 Gray/RGB/CMYK 统一转为 0~1 的 RGB 元组
        return min(float(color[0]), float(color[1]), float(color[2])) < WHITE_NORM
    if n == 4:
        # 兜底原始 CMYK：(0,0,0,0) 为纯白
        return max(float(v) for v in color) > (1.0 - WHITE_NORM)
    return any(float(v) < WHITE_NORM for v in color)


# ---------------------------------------------------------------- 像素级边界识别

def pixel_content_rect(page, dpi=72, threshold=DARK_THRESHOLD):
    """在可见页面坐标系 (page.rect) 下，按灰度像素识别真实非白内容边界。

    关键修复：
    1. 严格按有效像素宽度 `w` 切片 `sm[row_off : row_off + w]`，修复此前误将
       `pix.stride` 行末对齐填充字节（常为 0）当成黑色像素导致上下白边漏裁的 Bug。
    2. 对超大工程图（如加长 A0 图纸）动态限制采样最长边 <= 2600px，防止多核并发内存暴涨；
       对超小页面动态提升采样 DPI 保证精度。
    """
    pr = page.rect
    pw, ph = pr.width, pr.height
    if pw <= 0 or ph <= 0:
        return None

    max_pt = max(pw, ph)
    eff_dpi = float(dpi)
    max_px_target = 2600.0
    if max_pt * eff_dpi / 72.0 > max_px_target:
        eff_dpi = max(18.0, max_px_target * 72.0 / max_pt)
    elif max_pt < 180.0 and eff_dpi < 144.0:
        eff_dpi = 144.0
    eff_dpi = max(18, int(round(eff_dpi)))

    try:
        pix = page.get_pixmap(dpi=eff_dpi, colorspace=fitz.csGRAY, alpha=False)
    except Exception:
        return None
    w, h, st, sm = pix.width, pix.height, pix.stride, pix.samples
    if w < 2 or h < 2:
        return None

    # 第一次扫描：用 C 层实现的 min() 极速定位上下有内容的行
    top, bot = -1, -1
    for y in range(h):
        off = y * st
        if min(sm[off:off + w]) < threshold:
            if top < 0:
                top = y
            bot = y
    if top < 0:
        return None  # 全白页

    # 第二次扫描：仅在 [top, bot] 有效行内收缩左右边界（带剪枝：只需扫已发现的 left 左侧和 right 右侧）
    left, right = w, -1
    for y in range(top, bot + 1):
        off = y * st
        row = sm[off:off + w]
        # 仅需检查 [0, left) 是否存在更靠左的深色像素
        if left > 0 and min(row[:left]) < threshold:
            for x in range(left):
                if row[x] < threshold:
                    left = x
                    break
        # 仅需检查 (right, w) 是否存在更靠右的深色像素
        start_r = right + 1
        if start_r < w and min(row[start_r:]) < threshold:
            for x in range(w - 1, right, -1):
                if row[x] < threshold:
                    right = x
                    break
        if left == 0 and right == w - 1:
            break

    if right < 0 or right < left:
        return None

    sx = pw / w
    sy = ph / h
    return fitz.Rect(left * sx, top * sy, (right + 1) * sx, (bot + 1) * sy)


# ---------------------------------------------------------------- 矢量级与双引擎融合识别

def _scan_vectors_and_images(page, min_pt=1.0):
    """提取页面矢量元素与图片元素，并统一变换到可见页面坐标系 V (page.rect)。

    返回 (vec_rect_or_None, img_rect_or_None, max_single_img_area)
    """
    pr = page.rect
    pw, ph = pr.width, pr.height
    if pw <= 0 or ph <= 0:
        return None, None, 0.0

    rot = page.rotation % 360
    rot_mat = page.rotation_matrix if rot else None

    vx0 = vy0 = 1e30
    vx1 = vy1 = -1e30
    v_hit = False

    ix0 = iy0 = 1e30
    ix1 = iy1 = -1e30
    i_hit = False
    max_img_area = 0.0

    def _add_vec(rx0, ry0, rx1, ry1):
        nonlocal vx0, vy0, vx1, vy1, v_hit
        if rot_mat is not None:
            tr = fitz.Rect(rx0, ry0, rx1, ry1) * rot_mat
            rx0, ry0, rx1, ry1 = tr.x0, tr.y0, tr.x1, tr.y1
            if rx0 > rx1:
                rx0, rx1 = rx1, rx0
            if ry0 > ry1:
                ry0, ry1 = ry1, ry0
        # 夹紧到当前可见页面范围内，剔除框外隐藏冗余内容
        if rx0 < 0.0:
            rx0 = 0.0
        if ry0 < 0.0:
            ry0 = 0.0
        if rx1 > pw:
            rx1 = pw
        if ry1 > ph:
            ry1 = ph
        if rx1 <= rx0 and ry1 <= ry0:
            return
        # 仅当宽高同时小于 min_pt（孤立微尘点）才跳过；保留水平线(h=0)与垂直线(w=0)！
        if (rx1 - rx0) < min_pt and (ry1 - ry0) < min_pt:
            return
        if rx0 < vx0:
            vx0 = rx0
        if ry0 < vy0:
            vy0 = ry0
        if rx1 > vx1:
            vx1 = rx1
        if ry1 > vy1:
            vy1 = ry1
        v_hit = True

    # 1) 文字块（过滤掉纯空格/换行符幽灵块）
    try:
        for b in page.get_text("blocks"):
            btype = b[6] if len(b) > 6 else 0
            if btype == 0:
                txt = b[4] if len(b) > 4 else ""
                if not txt or not str(txt).strip():
                    continue
                if b[2] > b[0] and b[3] > b[1]:
                    _add_vec(b[0], b[1], b[2], b[3])
    except Exception:
        pass

    # 2) 矢量图元（支持 Clipping Path 蒙版裁剪 + 过滤纯白背景框/透明框 + 保留水平/垂直表格线）
    try:
        try:
            drawings = page.get_drawings(extended=True)
            has_ext = True
        except TypeError:
            drawings = page.get_drawings()
            has_ext = False

        # clip_stack 保存每一层级的累积裁剪框 (cx0, cy0, cx1, cy1)
        clip_stack = []
        for d in drawings:
            dtype = d.get("type")
            if has_ext:
                lvl = d.get("level", 0)
                if lvl < len(clip_stack):
                    del clip_stack[lvl:]
                if dtype == "clip":
                    sc = d.get("scissor")
                    if sc is not None and sc.is_valid:
                        cx0, cy0, cx1, cy1 = sc.x0, sc.y0, sc.x1, sc.y1
                        if clip_stack:
                            px0, py0, px1, py1 = clip_stack[-1]
                            if px0 > cx0:
                                cx0 = px0
                            if py0 > cy0:
                                cy0 = py0
                            if px1 < cx1:
                                cx1 = px1
                            if py1 < cy1:
                                cy1 = py1
                        while len(clip_stack) <= lvl:
                            clip_stack.append((cx0, cy0, cx1, cy1))
                    continue

            r = d.get("rect")
            if r is None or not r.is_valid:
                continue

            # 检查是否有肉眼可见的描边或填充（排除纯白底框与无色辅助框）
            stroke_w = d.get("width")
            has_stroke = (
                (stroke_w is None or stroke_w > 0)
                and _is_visible_color(d.get("color"), d.get("stroke_opacity", 1.0))
            )
            has_fill = _is_visible_color(d.get("fill"), d.get("fill_opacity", 1.0))
            if not has_stroke and not has_fill:
                continue

            rx0, ry0, rx1, ry1 = r.x0, r.y0, r.x1, r.y1
            if (rx1 - rx0) < min_pt and (ry1 - ry0) < min_pt:
                continue

            # 计入描边线宽的一半（上限 6pt），防止粗表格外框被贴中线切半
            if has_stroke and stroke_w and stroke_w > 0:
                hw = min(float(stroke_w) * 0.5, 6.0)
                rx0 -= hw
                ry0 -= hw
                rx1 += hw
                ry1 += hw

            # 受外层 Clipping Path 蒙版约束
            if clip_stack:
                cx0, cy0, cx1, cy1 = clip_stack[-1]
                if cx0 > rx0:
                    rx0 = cx0
                if cy0 > ry0:
                    ry0 = cy0
                if cx1 < rx1:
                    rx1 = cx1
                if cy1 < ry1:
                    ry1 = cy1
                if rx1 <= rx0 and ry1 <= ry0:
                    continue

            _add_vec(rx0, ry0, rx1, ry1)
    except Exception:
        pass

    # 3) 可见批注（电子红章/审图章/手写批注）与表单控件（排除透明超链接 Link=1 与隐藏项）
    try:
        annots = page.annots()
        if annots:
            for a in annots:
                atype = a.type[0] if isinstance(a.type, tuple) else a.type
                # 1=Link(透明点击区), 16=Popup(弹窗), 19=Watermark 之外；跳过 Invisible(1)/Hidden(2)/NoView(32)
                if atype in (1, 16):
                    continue
                if a.flags & (1 | 2 | 32):
                    continue
                ar = a.rect
                if ar is not None and ar.is_valid and ar.width > 0 and ar.height > 0:
                    _add_vec(ar.x0, ar.y0, ar.x1, ar.y1)
    except Exception:
        pass

    try:
        widgets = page.widgets()
        if widgets:
            for w in widgets:
                wr = w.rect
                if wr is not None and wr.is_valid and wr.width > 0 and wr.height > 0:
                    _add_vec(wr.x0, wr.y0, wr.x1, wr.y1)
    except Exception:
        pass

    # 4) 嵌入位图图片（单独统计，因为扫描图/截图内部自身可能带有大白边）
    try:
        for info in page.get_images(full=True):
            for r in page.get_image_rects(info[0]):
                if not (r.is_valid and r.width > 0 and r.height > 0):
                    continue
                rx0, ry0, rx1, ry1 = r.x0, r.y0, r.x1, r.y1
                if rot_mat is not None:
                    tr = fitz.Rect(rx0, ry0, rx1, ry1) * rot_mat
                    rx0, ry0, rx1, ry1 = tr.x0, tr.y0, tr.x1, tr.y1
                    if rx0 > rx1:
                        rx0, rx1 = rx1, rx0
                    if ry0 > ry1:
                        ry0, ry1 = ry1, ry0
                if rx0 < 0.0:
                    rx0 = 0.0
                if ry0 < 0.0:
                    ry0 = 0.0
                if rx1 > pw:
                    rx1 = pw
                if ry1 > ph:
                    ry1 = ph
                if rx1 <= rx0 or ry1 <= ry0:
                    continue
                area = (rx1 - rx0) * (ry1 - ry0)
                if area > max_img_area:
                    max_img_area = area
                if rx0 < ix0:
                    ix0 = rx0
                if ry0 < iy0:
                    iy0 = ry0
                if rx1 > ix1:
                    ix1 = rx1
                if ry1 > iy1:
                    iy1 = ry1
                i_hit = True
    except Exception:
        pass

    vec_rect = fitz.Rect(vx0, vy0, vx1, vy1) if v_hit and (vx1 > vx0 or vy1 > vy0) else None
    img_rect = fitz.Rect(ix0, iy0, ix1, iy1) if i_hit and ix1 > ix0 and iy1 > iy0 else None
    return vec_rect, img_rect, max_img_area


def page_content_rect(page, min_pt=1.0):
    """纯图元法计算页面可见坐标系 V (page.rect) 下的最小外接矩形。"""
    vec_rect, img_rect, _ = _scan_vectors_and_images(page, min_pt=min_pt)
    if vec_rect is not None and img_rect is not None:
        return vec_rect | img_rect
    return vec_rect if vec_rect is not None else img_rect


def page_content_rect_auto(page, min_pt=1.0, dpi=72):
    """智能双引擎融合识别页面内容真实边界（返回 page.rect 可见坐标系下的 fitz.Rect）。

    融合策略：
    - 文字与非白矢量图元（vec_rect）是 100% 确凿的真实墨迹（连 0.05pt 极细 CAD 线也能精准保留）；
    - 位图图片（img_rect）内部可能带有大片扫描白边（如整页扫描件、CAD 导出图片切片条）；
    - 若无图片且有确凿矢量内容：直接返回 vec_rect（< 2ms 极速，且不漏极细浅灰线）；
    - 若完全无图元（可能存在特殊栅格流）：回退像素法 pixel_content_rect；
    - 若图片撑大了外边界，且图片占比达到扫描件/大幅插图阈值（>= 12% 页面面积）或总覆盖 >= 92%：
      调用像素法剥离图片内部白边，并与确凿的 vec_rect 取并集！
    """
    pr = page.rect
    page_area = pr.width * pr.height
    if page_area <= 0:
        return None

    vec_rect, img_rect, _ = _scan_vectors_and_images(page, min_pt=min_pt)

    # 没有任何图元检出 → 交给像素法做最终确认
    if vec_rect is None and img_rect is None:
        return pixel_content_rect(page, dpi=dpi)

    # 纯矢量/文字页（不含位图图片）→ 已过滤纯白背景与蒙版，直接返回精准矢量边界
    if img_rect is None:
        # 若为单根水平线或垂直线导致某维宽度为 0，给 0.5pt 厚度保证非退化矩形
        x0, y0, x1, y1 = vec_rect.x0, vec_rect.y0, vec_rect.x1, vec_rect.y1
        if x1 - x0 < 0.5:
            x0 = max(0.0, x0 - 0.25)
            x1 = min(pr.width, x1 + 0.25)
        if y1 - y0 < 0.5:
            y0 = max(0.0, y0 - 0.25)
            y1 = min(pr.height, y1 + 0.25)
        return fitz.Rect(x0, y0, x1, y1)

    combined = (vec_rect | img_rect) if vec_rect is not None else img_rect

    # 判断图片是否向外撑大了矢量边界
    img_extends = (
        vec_rect is None
        or img_rect.x0 < vec_rect.x0 - 1.0
        or img_rect.y0 < vec_rect.y0 - 1.0
        or img_rect.x1 > vec_rect.x1 + 1.0
        or img_rect.y1 > vec_rect.y1 + 1.0
    )
    img_cover = (img_rect.width * img_rect.height) / page_area
    total_cover = (combined.width * combined.height) / page_area

    if img_extends and (img_cover >= IMG_CHECK_RATIO or total_cover >= PAGE_COVER_RATIO):
        pix = pixel_content_rect(page, dpi=dpi)
        if pix is not None:
            return (vec_rect | pix) if vec_rect is not None else pix
        # pix 为 None 说明图片全是白色像素！若也没有文字/矢量，则该页实为空白扫描页
        if vec_rect is None:
            return None
        return vec_rect

    return combined


# ---------------------------------------------------------------- 安全写回 CropBox

def apply_cropbox_safe(page, visible_rect):
    """将可见页面坐标系 V (page.rect) 中的裁剪框安全转换并写入 PDF CropBox。

    彻底解决 `ValueError: CropBox not in MediaBox` 的四大根源：
    1. 页面含有旋转（/Rotate 90 / 180 / 270）：通过 page.derotation_matrix 逆旋转回未旋转空间；
    2. MediaBox 原点非 (0, 0)（如 CAD 负坐标 [-200,-100,400,700] 或正偏移 [50,100,...]）：
       精确计算 MediaBox 在 PyMuPDF 顶部对齐空间 M 中的合法框 mb_m = Rect(mb.x0, 0, mb.x1, mb.height)；
    3. 页面已有旧 CropBox（甚至旧 CropBox 越出 MediaBox 的畸形 PDF）：
       先求有效基准框 eff_cb = page.cropbox & mb_m，叠加 (eff_cb.x0, eff_cb.y0) 精确平移；
    4. IEEE 754 float32/float64 边界舍入误差（mb.y1 - mr.y1 < mb.y0 差 1e-13）：
       通过 math.nextafter 逐 ULP 向内精确收敛，100% 满足 PyMuPDF _set_pagebox 断言。

    成功返回写回后对应的可见矩形 fitz.Rect，若裁剪框退化无效（宽或高 < 1pt）则不修改并返回 None。
    """
    pr = page.rect
    if pr.is_empty or pr.width < 1.0 or pr.height < 1.0:
        return None

    vr = fitz.Rect(visible_rect) & pr
    vr.normalize()
    if vr.is_empty or not vr.is_valid or vr.width < 1.0 or vr.height < 1.0:
        return None

    mb = fitz.Rect(page.mediabox)
    mb.normalize()
    if mb.is_empty or mb.width < 1.0 or mb.height < 1.0:
        return None

    # PyMuPDF _set_pagebox 的合法空间 M：x 范围 [mb.x0, mb.x1]，y 范围 [0.0, mb.height]
    mb_h = mb.y1 - mb.y0
    mb_m = fitz.Rect(mb.x0, 0.0, mb.x1, mb_h)

    # 当前页面的有效基准 CropBox（修正旧 CropBox 超出 MediaBox 时的原点偏差）
    raw_cb = fitz.Rect(page.cropbox)
    raw_cb.normalize()
    eff_cb = raw_cb & mb_m
    if eff_cb.is_empty or eff_cb.width <= 0.0 or eff_cb.height <= 0.0:
        eff_cb = mb_m

    # V (旋转后可见空间) -> U (未旋转有效 CropBox 相对空间)
    if page.rotation % 360 != 0:
        ur = vr * page.derotation_matrix
        ur.normalize()
    else:
        ur = fitz.Rect(vr)

    # U -> M (未旋转 MediaBox 顶部对齐空间)
    mr = fitz.Rect(
        ur.x0 + eff_cb.x0,
        ur.y0 + eff_cb.y0,
        ur.x1 + eff_cb.x0,
        ur.y1 + eff_cb.y0,
    )
    mr &= mb_m
    mr.normalize()

    if mr.is_empty or mr.width < 1.0 or mr.height < 1.0:
        return None

    # 精确满足 PyMuPDF _set_pagebox 内部不等式：
    # pdf_y0 = mb.y1 - mr.y1 >= mb.y0  且  pdf_y1 = mb.y1 - mr.y0 <= mb.y1
    x0, y0, x1, y1 = float(mr.x0), float(mr.y0), float(mr.x1), float(mr.y1)
    if x0 < mb.x0:
        x0 = float(mb.x0)
    if x1 > mb.x1:
        x1 = float(mb.x1)
    if y0 < 0.0:
        y0 = 0.0
    if y1 > mb_h:
        y1 = float(mb_h)

    for _ in range(16):
        if mb.y1 - y1 >= mb.y0 or y1 <= y0:
            break
        y1 = math.nextafter(y1, y0)
    for _ in range(16):
        if mb.y1 - y0 <= mb.y1 or y0 >= y1:
            break
        y0 = math.nextafter(y0, y1)

    if (x1 - x0) < 1.0 or (y1 - y0) < 1.0:
        return None

    final_mr = fitz.Rect(x0, y0, x1, y1)
    page.set_cropbox(final_mr)
    return vr


# ---------------------------------------------------------------- 多进程 Worker 接口

def init_worker(src, min_pt=1.0, dpi=72):
    """进程池初始化钩子：每个子进程仅打开一次 PDF，彻底消除大文件重复打开开销。"""
    _close_worker_doc()
    doc = fitz.open(src)
    if doc.needs_pass:
        doc.authenticate("")
    _WORKER_STATE["doc"] = doc
    _WORKER_STATE["src"] = src
    _WORKER_STATE["min_pt"] = min_pt
    _WORKER_STATE["dpi"] = dpi


def scan_single_page(page_idx):
    """扫描已驻留子进程内存中的单页，返回 (page_idx, (x0,y0,x1,y1) 或 None)。"""
    doc = _WORKER_STATE.get("doc")
    min_pt = _WORKER_STATE.get("min_pt", 1.0)
    dpi = _WORKER_STATE.get("dpi", 72)
    if doc is None:
        return (page_idx, None)
    try:
        r = page_content_rect_auto(doc[page_idx], min_pt=min_pt, dpi=dpi)
        box = (r.x0, r.y0, r.x1, r.y1) if r is not None else None
    except Exception:
        box = None
    return (page_idx, box)


def scan_chunk(args):
    """向后兼容接口：扫描 [start, end) 页区间，返回 [(页号, box), ...]。"""
    src, start, end, min_pt = args
    doc = fitz.open(src)
    if doc.needs_pass:
        doc.authenticate("")
    out = []
    try:
        for i in range(start, end):
            try:
                r = page_content_rect_auto(doc[i], min_pt=min_pt)
                box = (r.x0, r.y0, r.x1, r.y1) if r is not None else None
            except Exception:
                box = None
            out.append((i, box))
    finally:
        doc.close()
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        d = fitz.open(sys.argv[1])
        res = scan_chunk((sys.argv[1], 0, min(5, d.page_count), 1.0))
        d.close()
        for idx, b in res:
            print(f"page {idx + 1}: {b}")
