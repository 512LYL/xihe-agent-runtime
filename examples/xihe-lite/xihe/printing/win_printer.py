# -*- coding: utf-8 -*-
"""Windows 驱动渲染打印（唯一真正接触打印机的地方）

为什么不能用纯 IPP：
  这类机型的 IPP 只接受 application/PCLm、application/octet-stream、
  image/pwg-raster、image/urf；「IPP + application/pdf」被拒（status 1034），
  「octet-stream + PDF」能收但打出横向条纹乱码，「text/plain」直接拒。
  所以统一走 Windows 驱动渲染后交给打印队列。

双面打印怎么生效（这一条踩过坑）：
  `CreatePrinterDC` 用的是**打印机默认配置**，改了 GetPrinter 取到的 DEVMODE 也不生效。
  必须用**带 DEVMODE 的 `CreateDC(driver, device, devmode)`**，Duplex / 纸源才随
  本次任务生效，而且不需要管理员权限、不改打印机全局设置。

依赖：pywin32、pypdfium2、pillow（缺任何一个都只会让真机路径失败，
      模拟模式与状态机不受影响）。
"""
import os
import threading

# 全局锁：并发打印时 DEVMODE 会被互相篡改
_LOCK = threading.Lock()

DMDUP = {"simplex": 1, "long-edge": 2, "short-edge": 3}   # DEVMODE.dmDuplex


def list_printers():
    try:
        import win32print
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        return [p[2] for p in win32print.EnumPrinters(flags)]
    except Exception:
        return []


def resolve(printer_name=""):
    import win32print
    return printer_name or win32print.GetDefaultPrinter()


def _make_dc(printer_name, duplex_code, paper_source):
    """建一个带本次任务 Duplex / 纸源的 DC。返回 (dc, name, dpi, w, h)。"""
    import win32con
    import win32gui
    import win32print
    import win32ui

    name = resolve(printer_name)
    h_pri = win32print.OpenPrinter(name)
    try:
        props = win32print.GetPrinter(h_pri, 2)
        devmode = props["pDevMode"]
        driver = props["pDriverName"]
        devmode.Duplex = duplex_code
        if paper_source:
            devmode.DefaultSource = int(paper_source)
    finally:
        win32print.ClosePrinter(h_pri)

    hdc = win32gui.CreateDC(driver, name, devmode)
    dc = win32ui.CreateDCFromHandle(hdc)
    dpi = dc.GetDeviceCaps(win32con.LOGPIXELSY)
    return dc, name, dpi, dc.GetDeviceCaps(win32con.HORZRES), dc.GetDeviceCaps(win32con.VERTRES)


def print_text(text, printer_name="", paper_source=15, font_name="SimSun",
               font_pt=12, job_name="xihe-lite-text", duplex="simplex"):
    """把纯文本交给驱动排版打印（自动折行、自动分页）。返回 bool。"""
    try:
        import win32ui
    except Exception as e:
        print("[X] 未安装 pywin32：%s" % e)
        return False
    try:
        with _LOCK:
            dc, name, dpi, pw, ph = _make_dc(printer_name, DMDUP.get(duplex, 1),
                                             paper_source)
        margin = int(dpi * 0.5)
        line_h = int(dpi * font_pt / 72 * 1.6)
        char_w = max(1, int(dpi * font_pt / 72 * 0.95))    # 中文字宽约等于字号
        max_chars = max(1, (pw - 2 * margin) // char_w)
        font = win32ui.CreateFont({"name": font_name,
                                   "height": -int(dpi * font_pt / 72)})
        try:
            dc.StartDoc(job_name)
            dc.SelectObject(font)
            dc.StartPage()
            y = margin
            for raw in (text or "").split("\n"):
                chunks = [raw[i:i + max_chars]
                          for i in range(0, len(raw), max_chars)] or [""]
                for chunk in chunks:
                    if y + line_h > ph - margin:
                        dc.EndPage()
                        dc.StartPage()
                        y = margin
                    dc.TextOut(margin, y, chunk)
                    y += line_h
            dc.EndPage()
            dc.EndDoc()
            return True
        finally:
            dc.DeleteDC()
    except Exception as e:
        print("[X] 文本打印失败：%s: %s" % (type(e).__name__, e))
        return False


def print_pdf(pdf_path, printer_name="", paper_source=15, job_name=None,
              duplex="simplex"):
    """把 PDF 逐页渲染成位图交给驱动。返回 bool。

    双面时若页数为奇数，把**最后一页复制一份**补成偶数页，不留空白背面。
    """
    if not os.path.isfile(pdf_path):
        print("[X] 文件不存在：%s" % pdf_path)
        return False
    try:
        import pypdfium2 as pdfium
        from PIL import ImageWin
    except Exception as e:
        print("[X] 缺 PDF 渲染依赖（pypdfium2 / pillow）：%s" % e)
        return False
    try:
        with _LOCK:
            dc, name, dpi, pw, ph = _make_dc(printer_name, DMDUP.get(duplex, 1),
                                             paper_source)
        pdf = pdfium.PdfDocument(pdf_path)
        job_name = job_name or os.path.basename(pdf_path)
        n = len(pdf)
        dup_last = (DMDUP.get(duplex, 1) != 1) and (n % 2 == 1)
        pages = list(range(n)) + ([n - 1] if dup_last else [])
        try:
            dc.StartDoc(job_name)
            for i in pages:
                img = pdf[i].render(scale=dpi / 72).to_pil().convert("RGB")
                nw, nh = pw, int(img.height * pw / img.width)
                if nh > ph:                     # 超出可打印高度则整体缩放
                    nh, nw = ph, int(img.width * ph / img.height)
                img = img.resize((nw, nh))
                dc.StartPage()
                ImageWin.Dib(img).draw(dc.GetHandleOutput(), (0, 0, nw, nh))
                dc.EndPage()
            dc.EndDoc()
            return True
        finally:
            dc.DeleteDC()
            pdf.close()
    except Exception as e:
        print("[X] PDF 打印失败：%s: %s" % (type(e).__name__, e))
        return False
