#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""并行扫描子任务专用模块。

**为什么单独拆一个文件？**
Windows 的 multiprocessing 用 spawn：子进程会重新导入主模块。
如果把worker 函数放在 auto_crop_pdf.py 里，子进程导入时会再次执行整个文件，
导致 sys.argv 被改写、误走 GUI 分支，甚至无限递归启动。

拆成独立模块后，子进程只导入这一个干净的文件，不含任何界面逻辑，
`if __name__ == "__main__"` 分支也不会被触发，spawn 才真正可用。

这个文件不要单独运行，它由 auto_crop_pdf.py 自动调用。
"""

try:
    import pymupdf as fitz
except ImportError:
    import fitz


DARK_THRESHOLD = 245
PAGE_COVER_RATIO = 0.92


def _pixel_rect(page, dpi=72, threshold=DARK_THRESHOLD):
    """按像素内容判断真实边界（等同 WPS「智能选区」）。

    灰度 pixmap + 逐行 min()，实测 1191x2105 页约 0.016 秒。
    不要改成逐像素三层通道比较，会慢 75 倍。
    """
    try:
        pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    except Exception:
        return None
    if pix.width < 2 or pix.height < 2:
        return None

    sm, st, w, h = pix.samples, pix.stride, pix.width, pix.height

    top, bot = -1, -1
    for y in range(h):
        if min(sm[y * st:y * st + st]) < threshold:
            if top < 0:
                top = y
            bot = y
    if top < 0:
        return None

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
    return (left * pr.width / w, top * pr.height / h,
            (right + 1) * pr.width / w, (bot + 1) * pr.height / h)


def scan_chunk(args):
    """打开文档、扫描 [start, end) 页，返回 [(页号, (x0,y0,x1,y1) 或 None), ...]。"""
    src, start, end, min_pt = args

    doc = fitz.open(src)
    out = []
    try:
        for i in range(start, end):
            box = None
            try:
                page = doc[i]
                pr = page.rect
                page_area = pr.width * pr.height

                x0 = y0 = 1e30
                x1 = y1 = -1e30
                hit = False

                for b in page.get_text("blocks"):
                    if b[2] > b[0] and b[3] > b[1]:
                        if b[0] < x0:
                            x0 = b[0]
                        if b[1] < y0:
                            y0 = b[1]
                        if b[2] > x1:
                            x1 = b[2]
                        if b[3] > y1:
                            y1 = b[3]
                        hit = True

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

                if hit and x1 > x0 and y1 > y0:
                    # 图元几乎铺满整页 → 扫描件，图元坐标不可用，改看像素
                    if page_area > 0:
                        cover = (x1 - x0) * (y1 - y0) / page_area
                        if cover >= PAGE_COVER_RATIO:
                            box = _pixel_rect(page)
                        else:
                            box = (x0, y0, x1, y1)
                    else:
                        box = (x0, y0, x1, y1)
                else:
                    # 没有图元，可能仍存在扫描图
                    box = _pixel_rect(page)
            except Exception:
                box = None
            out.append((i, box))
    finally:
        doc.close()
    return out


if __name__ == "__main__":
    # 单独运行时自检：python _pdfcrop_worker.py 1.pdf
    import sys
    if len(sys.argv) > 1:
        d = fitz.open(sys.argv[1])
        r = scan_chunk((sys.argv[1], 0, min(3, d.page_count), 1.0))
        d.close()
        for i, box in r:
            print(f"page {i+1}: {box}")