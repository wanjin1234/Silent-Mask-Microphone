#!/usr/bin/env python3
"""把 docs/使用说明.md 渲染成 PDF。

在树莓派上也能重建（需要 reportlab：`pip3 install reportlab` 或
`sudo apt install python3-reportlab`）：

    python3 docs/build_manual_pdf.py

字体优先用系统里的中文字体；找不到时退回 reportlab 自带的 CID 字体
（STSong-Light），因此没有中文字体的系统也能生成，只是显示效果依赖阅读器。
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date

try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (BaseDocTemplate, Frame, KeepTogether, PageTemplate, Paragraph,
                                    Preformatted, Spacer, Table, TableStyle)
except ImportError:  # pragma: no cover
    sys.exit("需要 reportlab：pip3 install reportlab")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC_MD = os.path.join(HERE, "使用说明.md")
OUT_PDF = os.path.join(HERE, "使用说明.pdf")

TITLE = "4D 成像毫米波雷达 · 树莓派使用说明"
SUBTITLE = "Raspberry Pi 4B / Raspberry Pi OS · radarpi 1.0.0"

# 候选中文字体：(路径, ttc 内的字体序号)；粗体单独找
FONT_CANDIDATES = [
    (r"C:\Windows\Fonts\msyh.ttc", 0),
    (r"C:\Windows\Fonts\simsun.ttc", 0),
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # 没有中文时的兜底
]
FONT_BOLD_CANDIDATES = [
    (r"C:\Windows\Fonts\msyhbd.ttc", 0),
    r"C:\Windows\Fonts\simsunb.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def register_fonts() -> tuple:
    """注册正文字体，返回 (正文名, 粗体名, 是否真正支持中文)。"""
    body = None
    for cand in FONT_CANDIDATES:
        path, index = cand if isinstance(cand, tuple) else (cand, 0)
        if not os.path.exists(path):
            continue
        try:
            if path.lower().endswith(".ttc"):
                pdfmetrics.registerFont(TTFont("CJK", path, subfontIndex=index))
            else:
                pdfmetrics.registerFont(TTFont("CJK", path))
            body = "CJK"
            break
        except Exception:
            continue

    bold = body
    for cand in FONT_BOLD_CANDIDATES:
        path, index = cand if isinstance(cand, tuple) else (cand, 0)
        if not os.path.exists(path):
            continue
        try:
            if path.lower().endswith(".ttc"):
                pdfmetrics.registerFont(TTFont("CJK-Bold", path, subfontIndex=index))
            else:
                pdfmetrics.registerFont(TTFont("CJK-Bold", path))
            bold = "CJK-Bold"
            break
        except Exception:
            continue

    if body is None:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        return "STSong-Light", "STSong-Light", True

    pdfmetrics.registerFontFamily(body, normal=body, bold=bold, italic=body, boldItalic=bold)
    has_cjk = body == "CJK" and "DejaVu" not in str(FONT_CANDIDATES)
    return body, bold, has_cjk


# --------------------------------------------------------------------------
# 极简 Markdown 渲染（只覆盖本手册用到的语法）
# --------------------------------------------------------------------------

INLINE_CODE = re.compile(r"`([^`]+)`")
BOLD = re.compile(r"\*\*([^*]+)\*\*")
LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def inline(text: str, mono: str) -> str:
    """把行内语法转成 reportlab 的迷你 HTML（先整体转义，再套标签）。"""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = LINK.sub(lambda m: m.group(1), text)
    text = BOLD.sub(lambda m: "<b>%s</b>" % m.group(1), text)

    def code_sub(m):
        body = m.group(1)  # 已经转义过，不要再转义一次（否则 && 会变成 &amp;amp;）
        if all(ord(c) < 128 for c in body):
            return '<font name="%s" size="8.6">%s</font>' % (mono, body)
        # 含中文的行内代码用正文字体，避免等宽字体缺字变成方块
        return '<font size="8.8">%s</font>' % body

    return INLINE_CODE.sub(code_sub, text)


def parse_markdown(text: str):
    """把 markdown 拆成块：(类型, 内容)。"""
    blocks = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            blocks.append(("code", "\n".join(buf)))
            continue
        if stripped in ("---", "***", "___"):
            blocks.append(("hr", ""))
            i += 1
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            blocks.append(("h%d" % len(m.group(1)), m.group(2).strip()))
            i += 1
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c or "-") for c in cells):
                    rows.append(cells)
                i += 1
            blocks.append(("table", rows))
            continue
        if stripped.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append(("quote", " ".join(buf)))
            continue
        if re.match(r"^[-*]\s+", stripped) or re.match(r"^\d+\.\s+", stripped):
            items = []
            while i < len(lines):
                cur = lines[i].strip()
                m2 = re.match(r"^[-*]\s+(.*)$", cur) or re.match(r"^\d+\.\s+(.*)$", cur)
                if not m2:
                    break
                items.append(m2.group(1))
                i += 1
            blocks.append(("list", items))
            continue
        if not stripped:
            i += 1
            continue
        buf = [stripped]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(
            r"^(#{1,4}\s|```|\||>|[-*]\s|\d+\.\s|---)", lines[i].strip()
        ):
            buf.append(lines[i].strip())
            i += 1
        blocks.append(("p", " ".join(buf)))
    return blocks


# --------------------------------------------------------------------------


class Manual(BaseDocTemplate):
    """带页脚、页码与 PDF 书签的模板。"""

    def __init__(self, filename: str, body_font: str, bold_font: str):
        super().__init__(filename, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                         topMargin=20 * mm, bottomMargin=18 * mm,
                         title=TITLE, author="radarpi")
        frame = Frame(self.leftMargin, self.bottomMargin, self.width, self.height, id="body")
        self.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=self._footer)])
        self.body_font = body_font
        self.bold_font = bold_font
        self._anchors = []

    def _footer(self, canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont(self.body_font, 8)
        canvas.setFillColor(colors.HexColor("#666666"))
        canvas.drawString(18 * mm, 10 * mm, "radarpi 4D 成像毫米波雷达 · 树莓派使用说明")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, "第 %d 页" % doc.page)
        canvas.setStrokeColor(colors.HexColor("#cccccc"))
        canvas.line(18 * mm, 13 * mm, A4[0] - 18 * mm, 13 * mm)
        canvas.restoreState()

    def afterFlowable(self, flowable) -> None:
        """把标题注册成 PDF 书签，方便在阅读器里跳转。"""
        if hasattr(flowable, "_radarpi_level"):
            level, text = flowable._radarpi_level
            key = "h%d-%d" % (level, len(self._anchors))
            self._anchors.append(key)
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text, key, level=level - 1, closed=(level > 1))


def build() -> str:
    body, bold, _ = register_fonts()
    mono = "Courier"
    pdfmetrics.registerFontFamily("Courier", normal="Courier", bold="Courier-Bold",
                                  italic="Courier-Oblique", boldItalic="Courier-BoldOblique")

    with open(SRC_MD, "r", encoding="utf-8") as fh:
        md = fh.read()

    styles = getSampleStyleSheet()
    S = {
        "title": ParagraphStyle("t", fontName=bold, fontSize=22, leading=30, spaceAfter=6),
        "subtitle": ParagraphStyle("st", fontName=body, fontSize=12, leading=18,
                                   textColor=colors.HexColor("#555555"), spaceAfter=4),
        "h1": ParagraphStyle("h1", fontName=bold, fontSize=16, leading=24, spaceBefore=14,
                             spaceAfter=8, textColor=colors.HexColor("#1a3f66")),
        "h2": ParagraphStyle("h2", fontName=bold, fontSize=13.5, leading=20, spaceBefore=12,
                             spaceAfter=6, textColor=colors.HexColor("#1f5c8b")),
        "h3": ParagraphStyle("h3", fontName=bold, fontSize=12, leading=18, spaceBefore=10, spaceAfter=4),
        "h4": ParagraphStyle("h4", fontName=bold, fontSize=11, leading=16, spaceBefore=8, spaceAfter=3),
        "p": ParagraphStyle("p", fontName=body, fontSize=10, leading=16, alignment=TA_LEFT, spaceAfter=5),
        "li": ParagraphStyle("li", fontName=body, fontSize=10, leading=16, leftIndent=12,
                             bulletIndent=4, spaceAfter=3),
        "quote": ParagraphStyle("q", fontName=body, fontSize=9.5, leading=15, leftIndent=12,
                                rightIndent=6, textColor=colors.HexColor("#444444"), spaceAfter=6),
        "code": ParagraphStyle("c", fontName=mono, fontSize=8.4, leading=11.5,
                               textColor=colors.HexColor("#222222")),
        "code_cjk": ParagraphStyle("cc", fontName=body, fontSize=8.8, leading=12.5,
                                   textColor=colors.HexColor("#222222")),
        "cell": ParagraphStyle("cell", fontName=body, fontSize=8.6, leading=12),
        "cellh": ParagraphStyle("cellh", fontName=bold, fontSize=8.8, leading=12),
    }

    doc = Manual(OUT_PDF, body, bold)
    story = []
    story.append(Spacer(1, 40 * mm))
    story.append(Paragraph(TITLE, S["title"]))
    story.append(Paragraph(SUBTITLE, S["subtitle"]))
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph("生成日期：%s" % date.today().isoformat(), S["subtitle"]))
    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph(
        "本说明配合厂家《4D 成像毫米波雷达使用说明》使用：雷达的安装、供电、"
        "接线与安全要求仍以原手册为准。本说明覆盖树莓派上位机 radarpi 的安装、"
        "操作、配置、录制回放与故障排除。", S["p"]))
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph("原 Windows 上位机的固件升级（Upgrade）与校准向导"
                           "（Calibration）未移植，仍需 Windows 电脑操作；"
                           "其余功能在树莓派上等价可用。", S["quote"]))
    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph("目录", S["h2"]))
    toc_items = []
    for kind, content in parse_markdown(md):
        if kind == "h2" and content.strip() != "目录":
            toc_items.append(content)
    for item in toc_items:
        story.append(Paragraph("· %s" % item, S["li"]))
    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph("本 PDF 由 docs/build_manual_pdf.py 从 "
                           "docs/使用说明.md 自动生成，改文档请改 Markdown 源文件。", S["quote"]))

    from reportlab.platypus import PageBreak
    story.append(PageBreak())

    for kind, content in parse_markdown(md):
        if kind == "h1":
            p = Paragraph(content, S["h1"])
            p._radarpi_level = (1, content)
            story.append(p)
        elif kind == "h2":
            p = Paragraph(content, S["h2"])
            p._radarpi_level = (2, content)
            story.append(p)
        elif kind == "h3":
            p = Paragraph(content, S["h3"])
            p._radarpi_level = (3, content)
            story.append(p)
        elif kind == "h4":
            story.append(Paragraph(content, S["h4"]))
        elif kind == "p":
            story.append(Paragraph(inline(content, mono), S["p"]))
        elif kind == "quote":
            story.append(Paragraph(inline(content, mono), S["quote"]))
        elif kind == "list":
            for item in content:
                story.append(Paragraph(inline(item, mono), S["li"], bulletText="•"))
        elif kind == "hr":
            story.append(Spacer(1, 3 * mm))
        elif kind == "code":
            text = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            # 等宽字体（Courier）没有中文字形，含中文的代码块改用正文字体，
            # 否则注释会显示成一排方块
            style = S["code"] if all(ord(c) < 128 for c in content) else S["code_cjk"]
            tbl = Table([[Preformatted(text, style)]], colWidths=[doc.width])
            tbl.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f4f6f8")),
                ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#d5dbe2")),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.append(tbl)
            story.append(Spacer(1, 3 * mm))
        elif kind == "table":
            rows = content
            if not rows:
                continue
            ncols = max(len(r) for r in rows)
            data = []
            for ri, row in enumerate(rows):
                style = S["cellh"] if ri == 0 else S["cell"]
                cells = [Paragraph(inline(c, mono), style) for c in row]
                cells += [Paragraph("", style)] * (ncols - len(cells))
                data.append(cells)
            # 按内容长度分配列宽，避免窄列被挤成竖排
            weights = []
            for ci in range(ncols):
                longest = max(len(r[ci]) if ci < len(r) else 0 for r in rows)
                weights.append(max(longest, 6))
            total = sum(weights)
            widths = [doc.width * w / total for w in weights]
            tbl = Table(data, colWidths=widths, repeatRows=1)
            tbl.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c9d1d9")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f6")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafbfc")]),
            ]))
            story.append(KeepTogether(tbl) if len(rows) <= 6 else tbl)
            story.append(Spacer(1, 3 * mm))

    doc.build(story)
    return OUT_PDF


if __name__ == "__main__":
    path = build()
    print("已生成 %s（%.1f KB）" % (path, os.path.getsize(path) / 1024.0))
