"""PDF files: pdf_create (reportlab). Same input as docx_create (markdown or blocks), so a document can be made as Word or PDF
with no change. Reading PDFs is office_read; pictures of pages are office_render.

A Unicode TrueType font is used when one can be found (Arial / Segoe UI / Calibri on Windows, Liberation / DejaVu on Linux,
Arial on macOS, or LUCY_PDF_FONT=path\\to\\regular.ttf) so currency signs like the peso sign and accented letters print correctly. With
only the built-in Helvetica the tool reports characters it cannot show instead of silently printing black boxes.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

from . import _office as O
from .registry import ToolContext

HEAD = "#1F3864"
_FONT_SETS = [
    # (regular, bold, italic, bold-italic) candidates, first complete or at least regular+bold set wins
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/ariali.ttf", "C:/Windows/Fonts/arialbi.ttf"),
    ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/segoeuii.ttf", "C:/Windows/Fonts/segoeuiz.ttf"),
    ("C:/Windows/Fonts/calibri.ttf", "C:/Windows/Fonts/calibrib.ttf", "C:/Windows/Fonts/calibrii.ttf", "C:/Windows/Fonts/calibriz.ttf"),
    ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf", "/Library/Fonts/Arial Italic.ttf", "/Library/Fonts/Arial Bold Italic.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
     "/System/Library/Fonts/Supplemental/Arial Italic.ttf", "/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf"),
]
_fonts_cache: Optional[Dict[str, Any]] = None

SCHEMA_CREATE = {
    "type": "function",
    "function": {
        "name": "pdf_create",
        "description": ("Create a PDF from 'markdown' (easiest) or 'blocks', same format as docx_create. The file is sent to the chat; "
                        "to read a PDF use office_read."),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Where to save it, e.g. C:/Users/you/Documents/report.pdf"},
                "markdown": {"type": "string", "description": "The content: # Title, ## Heading, paragraphs, - bullets, 1. numbers, | tables |, ![caption](image path), [pagebreak]."},
                "blocks": {
                    "type": "array",
                    "description": "Instead of markdown: blocks {type: title|heading|para|bullets|numbered|table|image|pagebreak, text, level, items, rows, path, caption}.",
                    "items": {"type": "object", "properties": {"type": {"type": "string", "enum": ["title", "heading", "para", "bullets", "numbered", "table", "image", "pagebreak"]}}, "required": ["type"]},
                },
                "paper": {"type": "string", "enum": ["letter", "a4"], "description": "Paper size (default letter)."},
            },
            "required": ["path"],
        },
    },
}


def _find_fonts() -> Dict[str, Any]:
    """Register a Unicode font family with reportlab once. -> {'regular','bold','italic','bold_italic','mono','unicode'}"""
    global _fonts_cache
    if _fonts_cache is not None:
        return _fonts_cache
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    sets = list(_FONT_SETS)
    import os
    env = os.environ.get("LUCY_PDF_FONT", "").strip().strip('"')
    if env and Path(env).is_file():
        p = Path(env)
        sib = lambda *names: next((str(p.with_name(n)) for n in names if p.with_name(n).is_file()), str(p))     # noqa: E731
        sets.insert(0, (str(p), sib(p.stem + "bd" + p.suffix, p.stem + "-Bold" + p.suffix, p.stem + "b" + p.suffix),
                        sib(p.stem + "i" + p.suffix, p.stem + "-Italic" + p.suffix), sib(p.stem + "bi" + p.suffix, p.stem + "-BoldItalic" + p.suffix)))
    for reg, bold, ital, bi in sets:
        if not Path(reg).is_file():
            continue
        try:
            names = {"regular": "LucyReg", "bold": "LucyBold", "italic": "LucyItalic", "bold_italic": "LucyBoldItalic"}
            for key, f in (("regular", reg), ("bold", bold), ("italic", ital), ("bold_italic", bi)):
                pdfmetrics.registerFont(TTFont(names[key], f if Path(f).is_file() else reg))
            pdfmetrics.registerFontFamily("LucyReg", normal="LucyReg", bold="LucyBold", italic="LucyItalic", boldItalic="LucyBoldItalic")
            _fonts_cache = dict(names, mono="Courier", unicode=True)
            return _fonts_cache
        except Exception:                                            # noqa: BLE001 - broken font file: try the next family
            continue
    _fonts_cache = {"regular": "Helvetica", "bold": "Helvetica-Bold", "italic": "Helvetica-Oblique", "bold_italic": "Helvetica-BoldOblique",
                    "mono": "Courier", "unicode": False}
    return _fonts_cache


def _markup(text: str, mono: str) -> str:
    out = []
    for seg, b, i, code in O.parse_inline(text):
        seg = escape(seg)
        if code:
            seg = f'<font name="{mono}">{seg}</font>'
        if b:
            seg = f"<b>{seg}</b>"
        if i:
            seg = f"<i>{seg}</i>"
        out.append(seg)
    return "".join(out)


def _unshowable(text: str) -> List[str]:
    """Characters outside Latin-1 that built-in Helvetica cannot draw (typographic punctuation is fine: it maps to cp1252)."""
    ok = set("\u2018\u2019\u201c\u201d\u2013\u2014\u2022\u2026\u20ac\u2122")
    return sorted({c for c in text if ord(c) > 255 and c not in ok})


_NUMBERISH = re.compile(r"^[\(\-+]?\s*[$\u20ac\u00a3\u20b1\u00a5]?\s*[\d,]+(\.\d+)?\s*%?\)?$")


def _build(blocks: List[Dict[str, Any]], images: Dict[int, Path], paper: str, target_tmp: str, title: str) -> List[str]:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4, letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)

    f = _find_fonts()
    size = A4 if paper == "a4" else letter
    margin = 0.9 * inch
    head = colors.HexColor(HEAD)
    body = ParagraphStyle("body", fontName=f["regular"], fontSize=10.5, leading=15, spaceAfter=7, textColor=colors.HexColor("#222222"))
    styles = {
        "title": ParagraphStyle("title", parent=body, fontName=f["bold"], fontSize=24, leading=29, textColor=head, spaceAfter=10),
        "subtitle": ParagraphStyle("subtitle", parent=body, fontSize=13, leading=17, textColor=colors.HexColor("#595959"), spaceAfter=10),
        1: ParagraphStyle("h1", parent=body, fontName=f["bold"], fontSize=17, leading=21, textColor=head, spaceBefore=14, spaceAfter=6),
        2: ParagraphStyle("h2", parent=body, fontName=f["bold"], fontSize=13.5, leading=17, textColor=head, spaceBefore=11, spaceAfter=5),
        3: ParagraphStyle("h3", parent=body, fontName=f["bold"], fontSize=11.5, leading=15, textColor=head, spaceBefore=9, spaceAfter=4),
    }
    cell = ParagraphStyle("cell", parent=body, fontSize=9.5, leading=12.5, spaceAfter=0)
    frame_w = size[0] - 2 * margin
    story: List[Any] = []
    problems: List[str] = []
    alltext: List[str] = []

    def para(text: str, style, **kw):
        alltext.append(text)
        return Paragraph(_markup(text, f["mono"]), style, **kw)

    for bi, b in enumerate(blocks):
        t = b["type"]
        if t in ("title", "subtitle"):
            st = styles[t]
            if t == "title":
                st = ParagraphStyle("t2", parent=st, alignment=TA_LEFT)
            story.append(para(b["text"], st))
            if t == "title":
                from reportlab.platypus import HRFlowable
                story.append(HRFlowable(width="100%", thickness=1.2, color=head, spaceAfter=10))
        elif t == "heading":
            story.append(para(b["text"], styles[b["level"]]))
        elif t == "para":
            al = {"center": TA_CENTER, "right": TA_RIGHT, "justify": TA_JUSTIFY}.get(b.get("align"), TA_LEFT)
            st = ParagraphStyle("p", parent=body, alignment=al)
            txt = b["text"]
            if b.get("bold"):
                txt = f"**{txt}**"
            if b.get("italic"):
                txt = f"*{txt}*"
            story.append(para(txt, st))
        elif t in ("bullets", "numbered"):
            counters = [0, 0, 0]
            for it in b["items"]:
                lvl = it.get("level", 0)
                counters[lvl] += 1
                for deeper in range(lvl + 1, 3):
                    counters[deeper] = 0
                mark = "\u2022" if (t == "bullets" and lvl == 0) else "\u2013" if t == "bullets" else f"{counters[lvl]}."
                st = ParagraphStyle("li", parent=body, leftIndent=20 + 18 * lvl, bulletIndent=6 + 18 * lvl, spaceAfter=3, bulletFontName=f["regular"])
                story.append(para(it["text"], st, bulletText=mark))
            story.append(Spacer(1, 4))
        elif t == "table":
            rows = b["rows"]
            ncols = len(rows[0])
            lens = [max(4, min(40, max(len(O.strip_inline(r[c])) for r in rows))) for c in range(ncols)]
            if b.get("widths") and len(b["widths"]) == ncols:
                try:
                    lens = [max(0.1, float(x)) for x in b["widths"]]
                except (TypeError, ValueError):
                    pass
            col_w = [max(0.6 * inch, l / sum(lens) * frame_w) for l in lens]
            col_w = [w / sum(col_w) * frame_w for w in col_w]
            data = []
            for ri, r in enumerate(rows):
                row = []
                for ci, c in enumerate(r):
                    head_row = b["header"] and ri == 0
                    al = TA_RIGHT if (not head_row and _NUMBERISH.match(c.strip() or "x")) else TA_LEFT
                    st = ParagraphStyle("c", parent=cell, alignment=al, textColor=colors.white if head_row else colors.HexColor("#222222"),
                                        fontName=f["bold"] if head_row else f["regular"])
                    row.append(para(c, st))
                data.append(row)
            tbl = Table(data, colWidths=col_w, repeatRows=1 if b["header"] else 0)
            ts = [("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#BFBFBF")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                  ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
            if b["header"]:
                ts.append(("BACKGROUND", (0, 0), (-1, 0), head))
            for ri in range(2 if b["header"] else 1, len(rows), 2):
                ts.append(("BACKGROUND", (0, ri), (-1, ri), colors.HexColor("#F3F6FA")))
            tbl.setStyle(TableStyle(ts))
            story.append(tbl)
            story.append(Spacer(1, 10))
        elif t == "image":
            img = images[bi]
            w_px, h_px = O.image_size(img)
            maxw = min(frame_w, (float(b["width"]) * inch) if b.get("width") else frame_w * 0.9)
            maxh = size[1] - 2 * margin - 40
            sc = min(maxw / w_px, maxh / h_px)
            parts: List[Any] = [Image(str(img), width=w_px * sc, height=h_px * sc, hAlign="CENTER")]
            if b.get("caption"):
                parts.append(para(b["caption"], ParagraphStyle("cap", parent=body, alignment=TA_CENTER, fontSize=9, textColor=colors.HexColor("#595959"),
                                                               fontName=f["italic"])))
            story.append(KeepTogether(parts))
        elif t == "pagebreak":
            story.append(PageBreak())

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(f["regular"], 9)
        canvas.setFillColor(colors.HexColor("#7F7F7F"))
        canvas.drawCentredString(size[0] / 2, 0.5 * inch, str(doc.page))
        canvas.restoreState()

    doc = SimpleDocTemplate(target_tmp, pagesize=size, leftMargin=margin, rightMargin=margin, topMargin=margin, bottomMargin=margin,
                            title=title, author="Lucy")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    if not f["unicode"]:
        bad = _unshowable(" ".join(alltext))
        if bad:
            problems.append("These characters cannot be drawn with the built-in PDF font and show as boxes: " + " ".join(bad[:8]) +
                            ". Ask the user to install Arial / Segoe UI, set LUCY_PDF_FONT, or make a Word file instead.")
    return problems


def _create(args: dict, ctx: ToolContext) -> str:
    try:
        O.need("reportlab", "reportlab")
        target = O.target_path(args.get("path"), (".pdf",), must_exist=False, what="a PDF file")
        blocks = O.blocks_from(args)
        paper = str(args.get("paper") or "letter").strip().lower()
        paper = paper if paper in ("letter", "a4") else "letter"
        images: Dict[int, Path] = {}
        for i, b in enumerate(blocks):
            if b["type"] == "image":
                images[i] = O.image_file(b.get("path"), ctx.session_id, f"block {i + 1}")
        problems: List[str] = []

        def write(tmp: str) -> None:
            problems.extend(_build(blocks, images, paper, tmp, O.doc_title(blocks)))

        replaced = O.save_atomic(target, write)
        return O.finish(target, f"{len(blocks)} blocks" + (", replaced the old file" if replaced else ""), problems)
    except Exception as e:                                           # noqa: BLE001
        return O.fail(e)


TOOLS = [
    {"schema": SCHEMA_CREATE, "handler": _create, "tier": "write", "label": "write"},
]
