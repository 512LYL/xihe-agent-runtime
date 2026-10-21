# -*- coding: utf-8 -*-
"""文档 / 图片 → PDF

生产版把这一步散在 `print_doc_task.py`（Office COM）和 `image_compose.py`
（多图合成 A4）两个脚本里，都要靠子进程 + 环境变量传双面参数。精简版合成
一个模块，统一返回 `(pdf 路径, 错误说明)`：

    office_to_pdf(docx/xlsx)   Microsoft Office COM（Word/Excel）另存为 PDF
    images_to_pdf([png,jpg])   按 A4 排版合成一个多页 PDF（含 EXIF 摆正）
    page_count(pdf)            用 pypdfium2 数页（装不上返回 0）

所有失败都返回中文原因，不抛异常 —— 上层（驱动/自检）负责把它翻译成结论。
"""
import os
import tempfile

OFFICE_EXT = (".docx", ".doc", ".xlsx", ".xls")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")
TEXT_EXT = (".txt", ".md", ".log", ".csv", ".json")

A4_W_IN, A4_H_IN = 8.27, 11.69          # A4 英寸


def tmp_dir(cfg=None):
    from .. import config as _config
    base = _config.path(cfg, "paths", "run") if cfg else ""
    d = os.path.join(base, "tmp") if base else os.path.join(tempfile.gettempdir(),
                                                             "xihe-lite")
    os.makedirs(d, exist_ok=True)
    return d


def page_count(pdf_path):
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(pdf_path)
        try:
            return len(doc)
        finally:
            doc.close()
    except Exception:
        return 0


# ---------------------------------------------------------------- Office → PDF
def office_to_pdf(src, cfg=None):
    """Word/Excel 另存为 PDF。返回 (pdf 路径, 错误)。需要本机装 Office。"""
    ext = os.path.splitext(src)[1].lower()
    if ext not in OFFICE_EXT:
        return "", "不是 Office 文件：%s" % ext
    try:
        import win32com.client  # noqa: F401
    except Exception:
        return "", ("未安装 pywin32 / 本机无 Microsoft Office，无法把 %s 转成 PDF；"
                    "可直接打印 PDF 或纯文本" % ext)
    out = os.path.join(tmp_dir(cfg), "%s_print.pdf" % os.path.splitext(
        os.path.basename(src))[0])
    src_abs = os.path.abspath(src)
    app = None
    try:
        if ext in (".docx", ".doc"):
            app = win32com.client.Dispatch("Word.Application")
            app.Visible = False
            app.DisplayAlerts = 0
            doc = app.Documents.Open(src_abs, False, True)
            try:
                doc.SaveAs(out, FileFormat=17)        # wdFormatPDF
            finally:
                doc.Close(False)
        else:
            app = win32com.client.Dispatch("Excel.Application")
            app.Visible = False
            app.DisplayAlerts = False
            wb = app.Workbooks.Open(src_abs, 0, True)
            try:
                wb.ExportAsFixedFormat(0, out)        # xlTypePDF
            finally:
                wb.Close(False)
    except Exception as e:
        return "", "Office 转换失败（本机可能没装 Office）：%s: %s" % (
            type(e).__name__, str(e)[:120])
    finally:
        try:
            if app is not None:
                app.Quit()
        except Exception:
            pass
    if not os.path.exists(out):
        return "", "Office 转换未生成 PDF"
    return out, ""


# ---------------------------------------------------------------- 图片 → A4 PDF
def _load_image(path, rotate="auto"):
    from PIL import Image, ImageOps
    im = Image.open(path)
    try:
        im = ImageOps.exif_transpose(im)              # 按 EXIF 自动摆正
    except Exception:
        pass
    if rotate and rotate != "auto":
        deg = int(rotate) % 360
        if deg:
            im = im.rotate(-deg, expand=True)          # 顺时针语义
    return im.convert("RGB") if im.mode != "RGB" else im


def _fit_into(im, box_w, box_h, fit="contain"):
    from PIL import Image
    iw, ih = im.size
    if iw <= 0 or ih <= 0:
        raise ValueError("图片尺寸非法")
    if fit == "cover":
        scale = max(box_w / iw, box_h / ih)
        nw, nh = max(1, int(round(iw * scale))), max(1, int(round(ih * scale)))
        r = im.resize((nw, nh), Image.LANCZOS)
        left, top = max(0, (nw - box_w) // 2), max(0, (nh - box_h) // 2)
        return r.crop((left, top, left + box_w, top + box_h))
    scale = min(box_w / iw, box_h / ih)
    nw, nh = max(1, int(round(iw * scale))), max(1, int(round(ih * scale)))
    return im.resize((nw, nh), Image.LANCZOS)


def images_to_pdf(images, cfg=None, page="portrait", per_page=1, fit="contain",
                  rotate="auto", arrange="v", margin=30, dpi=200):
    """把多张图片按 A4 合成一个 PDF（每页 1 或 2 张）。返回 (pdf, 错误)。"""
    images = [p for p in (images or []) if os.path.isfile(p)]
    if not images:
        return "", "没有可用的图片"
    try:
        from PIL import Image
    except Exception:
        return "", "未安装 Pillow，无法合成图片 PDF（pip install pillow）"
    if per_page not in (1, 2):
        return "", "per_page 只支持 1 或 2"
    try:
        pw, ph = int(round(A4_W_IN * dpi)), int(round(A4_H_IN * dpi))
        if page == "landscape":
            pw, ph = ph, pw
        gap = max(0, margin)
        if per_page == 1:
            slots = [(margin, margin, pw - 2 * margin, ph - 2 * margin)]
        elif arrange == "h":
            sw = (pw - 2 * margin - gap) // 2
            slots = [(margin, margin, sw, ph - 2 * margin),
                     (margin + sw + gap, margin, pw - 2 * margin - sw - gap,
                      ph - 2 * margin)]
        else:
            sh = (ph - 2 * margin - gap) // 2
            slots = [(margin, margin, pw - 2 * margin, sh),
                     (margin, margin + sh + gap, pw - 2 * margin,
                      ph - 2 * margin - sh - gap)]
        pages = []
        for i in range(0, len(images), per_page):
            canvas = Image.new("RGB", (pw, ph), "white")
            for j, src in enumerate(images[i:i + per_page]):
                sx, sy, sw, sh = slots[j]
                fitted = _fit_into(_load_image(src, rotate), sw, sh, fit)
                fw, fh = fitted.size
                canvas.paste(fitted, (sx + (sw - fw) // 2, sy + (sh - fh) // 2))
            pages.append(canvas)
        out = os.path.join(tmp_dir(cfg), "imgs_%s.pdf" % (
            os.path.splitext(os.path.basename(images[0]))[0][:24] or "compose"))
        first, rest = pages[0], pages[1:]
        first.save(out, "PDF", save_all=True, append_images=rest,
                   resolution=float(dpi), quality=92)
        return out, ""
    except Exception as e:
        return "", "图片合成失败：%s: %s" % (type(e).__name__, e)
