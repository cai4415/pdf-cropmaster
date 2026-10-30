#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PDF-CropMaster 全链路回归测试套件（覆盖坐标系、旋转、双引擎、并发、占用与异常边界）。"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pymupdf as fitz
from _pdfcrop_worker import apply_cropbox_safe, page_content_rect_auto, pixel_content_rect
from auto_crop_pdf import (
    UserInputError,
    parse_drop_paths,
    parse_page_spec,
    process_one,
    unique_out,
)


class TestPDFCropMaster(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="pdfcrop_test_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_01_rotations_and_nonzero_mediabox_and_existing_cropbox(self):
        """复现并验证修复 `CropBox not in MediaBox`：全旋转角度 + 负坐标 MediaBox + 既有 CropBox。"""
        src = os.path.join(self.tmpdir, "墙体需求_复现.pdf")
        doc = fitz.open()
        for rot in (0, 90, 180, 270):
            p = doc.new_page(width=595, height=842)
            p.draw_rect(fitz.Rect(10, 10, 20, 20))
            xref_c = p.get_contents()[0]
            # 原始 PDF 坐标中绘制矩形 [80, 150, 320, 550] (宽 240, 高 400)
            doc.update_stream(xref_c, b"q 0 0 0 rg 80 150 240 400 re f Q")
            doc.xref_set_key(p.xref, "MediaBox", "[-100 -50 495 792]")
            doc.xref_set_key(p.xref, "CropBox", "[0 50 450 750]")
            doc.xref_set_key(p.xref, "Rotate", str(rot))
        # 第 5 页：横向旋转 90 度 + 整页扫描位图（之前必炸 CropBox not in MediaBox 的经典组合）
        p5 = doc.new_page(width=595, height=842)
        pm = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 595, 842), False)
        pm.clear_with(255)
        # 在未旋转位图的 (80..500, 62..742) 填充深色 -> 旋转 90 度后在可见横向页 (842x595) 中位于 x=100..780 (宽680), y=80..500 (高420)
        pm.set_rect(fitz.IRect(80, 62, 500, 742), (40, 40, 40))
        p5.insert_image(fitz.Rect(0, 0, 595, 842), pixmap=pm)
        p5.set_rotation(90)
        doc.save(src)
        doc.close()

        res = process_one(src, margin=5.0, unit="pt", workers=2)
        self.assertTrue(res["ok"], f"Expected ok, got error: {res['error']}")
        self.assertEqual(res["cropped"], 5)

        out_doc = fitz.open(res["out"])
        try:
            # 验证前 4 页旋转后内容均精准保留 5pt 四周边距
            expected_sizes = [(250, 410), (410, 250), (250, 410), (410, 250)]
            for idx, (ew, eh) in enumerate(expected_sizes):
                pr = out_doc[idx].rect
                self.assertAlmostEqual(pr.width, ew, delta=1.0)
                self.assertAlmostEqual(pr.height, eh, delta=1.0)
                r_after = page_content_rect_auto(out_doc[idx])
                self.assertAlmostEqual(r_after.x0, 5.0, delta=1.0)
                self.assertAlmostEqual(r_after.y0, 5.0, delta=1.0)
            # 验证第 5 页横向扫描页 (680+10 x 420+10 = 690 x 430)
            pr5 = out_doc[4].rect
            self.assertAlmostEqual(pr5.width, 690.0, delta=3.0)
            self.assertAlmostEqual(pr5.height, 430.0, delta=3.0)
        finally:
            out_doc.close()

    def test_02_out_of_bounds_existing_cropbox(self):
        """验证畸形 PDF（原 CropBox 超出 MediaBox）不崩溃且定位准确。"""
        src = os.path.join(self.tmpdir, "malformed_cb.pdf")
        doc = fitz.open()
        p = doc.new_page(width=595, height=842)
        p.draw_rect(fitz.Rect(100, 120, 300, 350), color=(0, 0, 0), fill=(0, 0, 0))
        doc.xref_set_key(p.xref, "MediaBox", "[0 0 595 842]")
        doc.xref_set_key(p.xref, "CropBox", "[-80 -90 700 950]")
        doc.save(src)
        doc.close()

        res = process_one(src, margin=10.0, unit="pt", workers=1)
        self.assertTrue(res["ok"], res["error"])
        d = fitz.open(res["out"])
        self.assertAlmostEqual(d[0].rect.width, 220.0, delta=1.5)
        self.assertAlmostEqual(d[0].rect.height, 250.0, delta=1.5)
        d.close()

    def test_03_white_bg_clip_path_hv_lines_and_blank_text(self):
        """验证：过滤纯白背景框 + Clip 蒙版裁剪 + 保留水平/垂直单直线 + 忽略纯空格文本。"""
        src = os.path.join(self.tmpdir, "table_and_clip.pdf")
        doc = fitz.open()
        p = doc.new_page(width=595, height=842)
        # 1) 占页面 85% 的纯白背景矩形（以前无法触发像素法又把白边撑满）
        p.draw_rect(fitz.Rect(20, 20, 575, 810), color=None, fill=(1, 1, 1))
        # 2) 页脚纯空格幽灵文字
        p.insert_text((30, 825), "      \n   ")
        # 3) 独立水平表格顶线 (y=120, x=90..480) 与独立垂直右框线 (x=480, y=120..620)
        p.draw_line((90, 120), (480, 120), color=(0, 0, 0), width=1.0)
        p.draw_line((480, 120), (480, 620), color=(0, 0, 0), width=1.0)
        p.insert_text((150, 300), "Table Data")

        # 第 2 页：带 Clipping Path 蒙版的大黑块（只露出 PDF 空间 [150, 300, 350, 500]）
        p2 = doc.new_page(width=595, height=842)
        p2.draw_rect(fitz.Rect(10, 10, 20, 20))
        xref_c2 = p2.get_contents()[0]
        doc.update_stream(xref_c2, b"q 150 300 200 200 re W n 0 0 0 rg 0 0 595 842 re f Q")
        doc.save(src)
        doc.close()

        res = process_one(src, margin=0.0, unit="pt", workers=1)
        self.assertTrue(res["ok"], res["error"])
        d = fitz.open(res["out"])
        # 第 1 页应紧贴 (90..480, 120..620) -> 391 x 501
        self.assertAlmostEqual(d[0].rect.width, 391.0, delta=2.0)
        self.assertAlmostEqual(d[0].rect.height, 501.0, delta=2.0)
        # 第 2 页应紧贴 clip 蒙版 200 x 200，而不是 595 x 842！
        self.assertAlmostEqual(d[1].rect.width, 200.0, delta=1.5)
        self.assertAlmostEqual(d[1].rect.height, 200.0, delta=1.5)
        d.close()

    def test_04_mixed_paper_sizes_uniform_mode(self):
        """验证 A4 竖向 + A3 横向混合文档在 uniform 模式下按规格分组，绝不切坏大图纸。"""
        src = os.path.join(self.tmpdir, "mixed_a4_a3.pdf")
        doc = fitz.open()
        for _ in range(3):
            p = doc.new_page(width=595, height=842)
            p.draw_rect(fitz.Rect(60, 70, 530, 770), color=(0, 0, 0))
        # 插入一页 A3 横向大图纸 (1191 x 842)
        pa3 = doc.new_page(width=1191, height=842)
        pa3.draw_rect(fitz.Rect(50, 50, 1140, 790), color=(0, 0, 0))
        doc.save(src)
        doc.close()

        res = process_one(src, margin=5.0, unit="pt", mode="uniform", workers=2)
        self.assertTrue(res["ok"], res["error"])
        d = fitz.open(res["out"])
        # A3 页宽度应为 1090 + 10 = 1100，绝不能被 A4 的中位数 480 切掉右半边！
        self.assertGreater(d[3].rect.width, 1090.0)
        d.close()

    def test_05_encrypted_pdf_handling(self):
        """验证加密 PDF 能给出清晰密码提示，而非误报为全白页。"""
        src = os.path.join(self.tmpdir, "encrypted.pdf")
        doc = fitz.open()
        p = doc.new_page(width=595, height=842)
        p.insert_text((100, 200), "Secret")
        doc.save(src, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="123456", owner_pw="654321")
        doc.close()

        res = process_one(src)
        self.assertFalse(res["ok"])
        self.assertFalse(res["skipped"])
        self.assertIn("密码", res["error"])

    def test_06_page_spec_and_negative_margin_validation(self):
        """验证中文标点页码解析、非法页码提示与负数边距拦截。"""
        self.assertEqual(parse_page_spec("1～3，5、7—8", 10), {0, 1, 2, 4, 6, 7})
        with self.assertRaises(UserInputError):
            parse_page_spec("1-abc", 10)
        with self.assertRaises(UserInputError):
            parse_page_spec("0", 10)

        src = os.path.join(self.tmpdir, "simple.pdf")
        doc = fitz.open()
        p = doc.new_page()
        p.insert_text((100, 100), "Hi")
        doc.save(src)
        doc.close()

        res_neg = process_one(src, margin=-10)
        self.assertFalse(res_neg["ok"])
        self.assertIn("不能为负数", res_neg["error"])

        res_same = process_one(src, out=src)
        self.assertFalse(res_same["ok"])
        self.assertIn("不能与源文件相同", res_same["error"])

    def test_07_folder_drop_and_locked_output_resilience(self):
        """验证拖入文件夹自动识别未裁剪 PDF，以及输出文件被锁定时自动改名保存。"""
        sub = os.path.join(self.tmpdir, "drop_folder")
        os.makedirs(sub, exist_ok=True)
        f1 = os.path.join(sub, "a.pdf")
        f2 = os.path.join(sub, "a_已裁剪.pdf")
        for fp in (f1, f2):
            d = fitz.open()
            pg = d.new_page()
            pg.insert_text((100, 100), "Test")
            d.save(fp)
            d.close()

        paths, ignored = parse_drop_paths(None, sub)
        self.assertEqual(paths, [os.path.abspath(f1)])
        self.assertEqual(ignored, 0)

        # Windows 文件锁测试
        if sys.platform == "win32":
            import msvcrt
            locked_out = os.path.join(self.tmpdir, "locked_out.pdf")
            with open(locked_out, "wb") as lf:
                lf.write(b"%PDF-1.4")
            lf = open(locked_out, "r+b")
            msvcrt.locking(lf.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                res = process_one(f1, out=locked_out, workers=1)
                self.assertTrue(res["ok"], f"Failed under file lock: {res['error']}")
                self.assertNotEqual(os.path.normcase(res["out"]), os.path.normcase(locked_out))
                self.assertTrue(os.path.isfile(res["out"]))
            finally:
                msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
                lf.close()

    def test_08_cli_dry_run_and_multiprocess(self):
        """验证 CLI 入口与多进程扫描完整链路（>=4 页触发真实 spawn 进程池）。"""
        src = os.path.join(self.tmpdir, "multi.pdf")
        doc = fitz.open()
        for i in range(6):
            p = doc.new_page(width=595, height=842)
            if i != 2:  # 第 3 页留白测试空白页跳过
                p.draw_rect(fitz.Rect(80 + i * 5, 90, 500, 750), color=(0, 0, 0))
        doc.save(src)
        doc.close()

        proc = subprocess.run(
            [sys.executable, "main.py", src, "--dry-run", "-j", "2"],
            cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
