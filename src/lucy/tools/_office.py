"""Helpers shared by the Office tools (docx / xlsx / pptx / pdf). Leading underscore: never treated as a tool module.

Design rule for a 4-12B model: it never writes library code. It sends STRUCTURED input (blocks / sheets / slides) or,
even easier, Markdown; everything below turns that into clean, forgiving, validated data. Mistakes come back as ONE short
message that says what is wrong and how to fix it (OfficeError), never a stack trace.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from lucy import paths as _paths

from ._common import outside_root, resolve_path

# Windows: never flash a console window when we start LibreOffice
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class OfficeError(Exception):
    """A problem the model can fix. str(e) is returned to it as the tool result (prefixed with 'Error: ')."""


def fail(e: Exception) -> str:
    return f"Error: {e}" if isinstance(e, OfficeError) else f"Error: {type(e).__name__}: {e}"


def need(module: str, pip_name: str):
    """Import an optional library or tell the user exactly what to install."""
    try:
        return __import__(module, fromlist=["_"])
    except ImportError as e:
        raise OfficeError(f"the '{pip_name}' package is not installed on this PC. Ask the user to run: pip install {pip_name}") from e


# ---------------------------------------------------------------------------------------------- paths
def target_path(path: Any, ext: Tuple[str, ...], *, must_exist: bool, what: str) -> Path:
    """Resolve what the model typed to a real file path with the right extension (added when missing)."""
    raw = str(path or "").strip().strip('"').strip("'")
    if not raw:
        raise OfficeError("'path' is missing. Give the full file path, e.g. C:/Users/you/Documents/report" + ext[0])
    if not Path(raw).suffix:
        raw += ext[0]
    if Path(raw).suffix.lower() not in ext:
        shown = " or ".join(ext) if len(ext) <= 3 else ", ".join(ext[:4]) + " ..."
        raise OfficeError(f"{what} must end with {shown} (got '{Path(raw).suffix}').")
    t = resolve_path(raw)
    if t is None:
        raise OfficeError(outside_root(raw))
    if must_exist and not t.is_file():
        raise OfficeError(f"file not found: {raw}. Check the path with find_file / list_local_files.")
    return t


def check_can_read(path: Path, session_id: Optional[str]) -> None:
    """Files named INSIDE tool arguments (images) are invisible to the policy, so ask it here: a blocked location
    (secrets, browser profiles...) must not be embeddable in a document that can then be sent anywhere."""
    try:
        from lucy import safety
        from lucy.safety.policy import DENY
        d = safety.get().decide("read_local_file", "read", {"file_path": path.as_posix()}, session_id)
        if d.level >= DENY:
            raise OfficeError(f"cannot use {path.name}: {d.reason}")
    except OfficeError:
        raise
    except Exception:                                    # noqa: BLE001 - no safety layer installed -> nothing to ask
        return


def image_file(raw: Any, session_id: Optional[str], where: str) -> Path:
    """An image the document should contain: found, readable by policy, really an image. Converted to PNG if the
    office format can't take it (webp, tiff, ...)."""
    p = str(raw or "").strip()
    if not p:
        raise OfficeError(f"{where}: image 'path' is missing.")
    t = resolve_path(p)
    if t is None or not t.is_file():
        raise OfficeError(f"{where}: image not found: {p}. Check the path with find_file.")
    try:
        check_can_read(t, session_id)
    except OfficeError as e:
        raise OfficeError(f"{where}: {e}") from e
    PIL = need("PIL.Image", "pillow")
    try:
        with PIL.open(t) as im:
            im.verify()
        with PIL.open(t) as im:
            fmt = (im.format or "").upper()
            size = im.size
            if fmt in ("PNG", "JPEG", "GIF", "BMP"):
                return t
            out = Path(tempfile.mkdtemp(prefix="lucy_img_")) / (t.stem + ".png")
            im.convert("RGBA" if "A" in im.getbands() else "RGB").save(out, "PNG")
            return out
    except OfficeError:
        raise
    except Exception as e:                               # noqa: BLE001
        raise OfficeError(f"{where}: {t.name} is not a usable image ({type(e).__name__}).") from e


def image_size(path: Path) -> Tuple[int, int]:
    PIL = need("PIL.Image", "pillow")
    with PIL.open(path) as im:
        return im.size


def save_atomic(target: Path, writer) -> bool:
    """writer(tmp_path) writes the file; it is moved over `target` only when complete. -> True if it replaced a file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".lucy_", suffix=target.suffix)
    os.close(fd)
    try:
        writer(tmp)
        try:
            os.replace(tmp, target)
        except PermissionError as e:
            raise OfficeError(f"{target.name} is open in another program (Word / Excel / PowerPoint / a PDF viewer). "
                              "Ask the user to close it, or save under a different file name.") from e
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return existed


def deliver(target: Path, headline: str = "") -> str:
    """The MEDIA: result the chat UI understands (file shows up in the chat and the Library). A path with spaces cannot
    travel in a MEDIA: line, so a copy with underscores is delivered instead."""
    shown = target
    if re.search(r"\s", target.as_posix()):
        d = _paths.RUNTIME / "tmp" / "deliver" / hashlib.sha1(target.as_posix().encode("utf-8")).hexdigest()[:8]
        d.mkdir(parents=True, exist_ok=True)
        shown = d / re.sub(r"\s+", "_", target.name)
        shutil.copy2(target, shown)
    try:
        s = shown.relative_to(_paths.SAFE_ROOT).as_posix()
    except ValueError:
        s = shown.as_posix()
    return f"MEDIA:{s}"


def finish(target: Path, summary: str, problems: List[str]) -> str:
    """No problems -> deliver the file to the chat (the model only sees 'delivered'). Problems -> a plain text result so
    the model SEES them: the file is saved but not yet delivered; fixing = call the tool again (same path replaces it)."""
    if not problems:
        return deliver(target)
    return (f"Saved {target.as_posix()} ({summary}) but NOT sent to the chat yet, because: " + " ".join(problems) +
            " Fix it by calling the same tool again with the same path (it replaces the file), or call send_file to send it as is.")


# ---------------------------------------------------------------------------------------------- inline markup
_INLINE = re.compile(r"(\*\*[^*\n]+?\*\*|__[^_\n]+?__|\*[^*\s][^*\n]*?\*|`[^`\n]+`)")


def parse_inline(text: str) -> List[Tuple[str, bool, bool, bool]]:
    """'a **bold** and *italic* and `code`' -> [(text, bold, italic, code)]. Unmatched markers stay as plain text."""
    out: List[Tuple[str, bool, bool, bool]] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False, False, False))
        tok = m.group(0)
        if tok.startswith(("**", "__")):
            out.append((tok[2:-2], True, False, False))
        elif tok.startswith("`"):
            out.append((tok[1:-1], False, False, True))
        else:
            out.append((tok[1:-1], False, True, False))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False, False, False))
    return out or [("", False, False, False)]


def strip_inline(text: str) -> str:
    return "".join(t for t, *_ in parse_inline(str(text)))


# ---------------------------------------------------------------------------------------------- blocks
BLOCK_TYPES = ("heading", "para", "bullets", "numbered", "table", "image", "pagebreak")
_TYPE_ALIASES = {
    "title": "title", "h": "heading", "h1": "heading", "h2": "heading", "h3": "heading", "header": "heading",
    "subheading": "heading", "section": "heading", "subtitle": "subtitle",
    "paragraph": "para", "p": "para", "text": "para", "body": "para", "line": "para",
    "bullet": "bullets", "bulleted": "bullets", "list": "bullets", "ul": "bullets", "unordered": "bullets", "bulletlist": "bullets",
    "number": "numbered", "numbers": "numbered", "ol": "numbered", "ordered": "numbered", "numberedlist": "numbered",
    "numbered_list": "numbered", "bullet_list": "bullets", "tbl": "table", "grid": "table", "picture": "image", "img": "image",
    "figure": "image", "page_break": "pagebreak", "break": "pagebreak", "pagebr": "pagebreak", "newpage": "pagebreak",
}
_HEAD_LEVEL = {"h1": 1, "h2": 2, "h3": 3}


def _s(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _items(v: Any) -> List[Dict[str, Any]]:
    """['a', 'b'] / [{'text': 'a', 'level': 1}] / ['a', ['sub1', 'sub2']] / 'a\\nb' -> [{'text', 'level'}]"""
    out: List[Dict[str, Any]] = []

    def walk(x: Any, level: int) -> None:
        if isinstance(x, (list, tuple)):
            for y in x:
                walk(y, level + (1 if isinstance(y, (list, tuple)) else 0))
        elif isinstance(x, dict):
            t = x.get("text", x.get("content", x.get("item", "")))
            out.append({"text": _s(t), "level": max(0, min(2, int(x.get("level", level) or 0)))})
            if x.get("items") or x.get("children"):
                walk(x.get("items") or x.get("children"), level + 1)
        elif x is not None and _s(x).strip() != "":
            txt = re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s+", "", _s(x).strip()) if not isinstance(x, (int, float)) else _s(x)
            out.append({"text": txt, "level": max(0, min(2, level))})

    if isinstance(v, str):
        v = [ln for ln in v.splitlines() if ln.strip()]
        v = [re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s+", "", ln) for ln in v]
    walk(v, 0)
    return out


def _rows(v: Any) -> List[List[str]]:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in v.splitlines() if ln.strip()]
    if isinstance(v, list) and v and all(isinstance(r, dict) for r in v):
        keys: List[str] = []
        for r in v:
            for k in r:
                if k not in keys:
                    keys.append(k)
        v = [keys] + [[r.get(k, "") for k in keys] for r in v]
    if not isinstance(v, list) or not v:
        raise OfficeError("a table needs 'rows': a list of rows, each a list of cell texts, e.g. [[\"Name\",\"Qty\"],[\"Pens\",\"4\"]].")
    rows = [[_s(c) for c in (r if isinstance(r, (list, tuple)) else [r])] for r in v]
    width = max(len(r) for r in rows)
    return [r + [""] * (width - len(r)) for r in rows]


def _as_list(v: Any, what: str) -> list:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError as e:
            raise OfficeError(f"'{what}' must be a list, not text. Send a JSON list (or use the 'markdown' field).") from e
    if isinstance(v, dict):
        v = [v]
    if not isinstance(v, list):
        raise OfficeError(f"'{what}' must be a list.")
    return v


def norm_blocks(blocks: Any) -> List[Dict[str, Any]]:
    """Forgiving normaliser: aliases, string blocks, h1/h2 -> heading levels, list/row shapes. Raises OfficeError that names
    the offending block number."""
    out: List[Dict[str, Any]] = []
    for i, b in enumerate(_as_list(blocks, "blocks"), 1):
        where = f"block {i}"
        if isinstance(b, str):
            out.extend(parse_markdown(b) if "\n" in b or b.lstrip().startswith(("#", "-", "*", "|")) else [{"type": "para", "text": b}])
            continue
        if not isinstance(b, dict):
            raise OfficeError(f"{where}: each block must be an object like {{\"type\": \"para\", \"text\": \"...\"}}.")
        raw_t = str(b.get("type") or b.get("kind") or "").strip().lower().replace(" ", "")
        if not raw_t:
            raw_t = "bullets" if "items" in b else "table" if "rows" in b else "image" if "path" in b or "src" in b else "para"
        t = _TYPE_ALIASES.get(raw_t, raw_t)
        if t not in BLOCK_TYPES + ("title", "subtitle"):
            raise OfficeError(f"{where}: unknown type '{raw_t}'. Use one of: title, heading, para, bullets, numbered, table, image, pagebreak.")
        text = _s(b.get("text", b.get("content", b.get("title", ""))))
        if t in ("heading", "title", "subtitle", "para"):
            if not text.strip():
                raise OfficeError(f"{where} ({t}): 'text' is empty.")
            nb: Dict[str, Any] = {"type": t, "text": text}
            if t == "heading":
                lvl = b.get("level", _HEAD_LEVEL.get(raw_t, 1))
                try:
                    nb["level"] = max(1, min(3, int(lvl)))
                except (TypeError, ValueError):
                    nb["level"] = 1
            if t == "para":
                al = str(b.get("align", "")).lower()
                if al in ("left", "center", "centre", "right", "justify"):
                    nb["align"] = "center" if al == "centre" else al
                nb["bold"], nb["italic"] = bool(b.get("bold")), bool(b.get("italic"))
            out.append(nb)
        elif t in ("bullets", "numbered"):
            its = _items(b.get("items", b.get("list", b.get("content", b.get("text", "")))))
            if not its:
                raise OfficeError(f"{where} ({t}): 'items' is empty. Give a list of strings.")
            out.append({"type": t, "items": its})
        elif t == "table":
            try:
                rows = _rows(b.get("rows", b.get("data", b.get("cells"))))
            except OfficeError as e:
                raise OfficeError(f"{where}: {e}") from e
            out.append({"type": "table", "rows": rows, "header": b.get("header", True) not in (False, "false", "no", 0),
                        "widths": b.get("widths") if isinstance(b.get("widths"), list) else None})
        elif t == "image":
            p = b.get("path", b.get("src", b.get("file", b.get("image", ""))))
            w = b.get("width", b.get("width_inches"))
            try:
                w = float(w) if w not in (None, "") else None
            except (TypeError, ValueError):
                w = None
            out.append({"type": "image", "path": _s(p), "width": w, "caption": _s(b.get("caption", b.get("alt", "")))})
        else:
            out.append({"type": "pagebreak"})
    if not out:
        raise OfficeError("no content: 'blocks' (or 'markdown') is empty.")
    return out


_MD_HEAD = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_MD_BUL = re.compile(r"^(\s*)(?:[-*+\u2022])\s+(.+)$")
_MD_NUM = re.compile(r"^(\s*)\d+[.)]\s+(.+)$")
_MD_IMG = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$")
_MD_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_MD_BREAK = re.compile(r"^\s*(?:\[pagebreak\]|<pagebreak\s*/?>|\\pagebreak|\\newpage|-{3,}\s*pagebreak\s*-{3,})\s*$", re.I)


def _md_cells(line: str) -> List[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_markdown(md: str) -> List[Dict[str, Any]]:
    """Small Markdown -> blocks. # headings (first '# ' of the text becomes the document title when it comes first),
    - bullets / 1. numbers (indent = sub level), | tables |, ![alt](path), [pagebreak], blank-line separated paragraphs."""
    blocks: List[Dict[str, Any]] = []
    para: List[str] = []
    lines = str(md or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")

    def flush() -> None:
        if para:
            blocks.append({"type": "para", "text": " ".join(x.strip() for x in para)})
            para.clear()

    def add_item(kind: str, indent: str, text: str) -> None:
        flush()
        lvl = min(2, len(indent.replace("\t", "    ")) // 2)
        if blocks and blocks[-1]["type"] == kind and not blocks[-1].get("_closed"):
            blocks[-1]["items"].append({"text": text.strip(), "level": lvl})
        else:
            blocks.append({"type": kind, "items": [{"text": text.strip(), "level": lvl}]})

    i = 0
    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            flush()
            if blocks and blocks[-1]["type"] in ("bullets", "numbered"):
                blocks[-1]["_closed"] = True
            i += 1
            continue
        if _MD_BREAK.match(ln):
            flush(); blocks.append({"type": "pagebreak"}); i += 1; continue
        m = _MD_HEAD.match(ln)
        if m:
            flush()
            n = len(m.group(1))
            if n == 1 and not blocks:
                blocks.append({"type": "title", "text": m.group(2).strip()})
            else:
                blocks.append({"type": "heading", "level": max(1, min(3, n - 1 if n > 1 and any(b.get("type") == "title" for b in blocks) else n)),
                               "text": m.group(2).strip()})
            i += 1
            continue
        m = _MD_IMG.match(ln.strip())
        if m:
            flush(); blocks.append({"type": "image", "path": m.group(2).strip(), "caption": m.group(1).strip(), "width": None}); i += 1; continue
        if "|" in ln and i + 1 < len(lines) and _MD_SEP.match(lines[i + 1]):
            flush()
            rows = [_md_cells(ln)]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_md_cells(lines[i])); i += 1
            w = max(len(r) for r in rows)
            blocks.append({"type": "table", "rows": [r + [""] * (w - len(r)) for r in rows], "header": True, "widths": None})
            continue
        m = _MD_BUL.match(ln)
        if m:
            add_item("bullets", m.group(1), m.group(2)); i += 1; continue
        m = _MD_NUM.match(ln)
        if m:
            add_item("numbered", m.group(1), m.group(2)); i += 1; continue
        para.append(ln)
        i += 1
    flush()
    for b in blocks:
        b.pop("_closed", None)
    return blocks


def blocks_from(args: dict) -> List[Dict[str, Any]]:
    """`blocks` (structured) or `markdown` (easiest for a small model) -> normalised blocks."""
    md, blocks = args.get("markdown"), args.get("blocks")
    if blocks not in (None, "", []):
        return norm_blocks(blocks)
    if isinstance(md, str) and md.strip():
        return norm_blocks(parse_markdown(md))
    raise OfficeError("nothing to write: give 'blocks' (list) or 'markdown' (text with # headings, - bullets, | tables |).")


def doc_title(blocks: List[Dict[str, Any]]) -> str:
    for b in blocks:
        if b["type"] in ("title", "heading"):
            return strip_inline(b["text"])[:120]
    return ""


# ---------------------------------------------------------------------------------------------- page ranges
def page_range(spec: Any, total: int, default: Optional[Tuple[int, int]] = None, max_pages: int = 12) -> List[int]:
    """'1-3,5' -> [1,2,3,5] (1-based, clipped to the document, at most max_pages). Empty spec -> default or everything."""
    s = str(spec or "").strip().lower()
    if not s or s in ("all", "*"):
        lo, hi = default or (1, total)
        return list(range(max(1, lo), min(total, hi) + 1))[:max_pages]
    pages: List[int] = []
    for part in re.split(r"[,\s]+", s):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*(?:-|\.\.|to)\s*(\d+)", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            pages += list(range(min(lo, hi), max(lo, hi) + 1))
        elif part.isdigit():
            pages.append(int(part))
        else:
            raise OfficeError(f"cannot read the page list '{spec}'. Use e.g. 2 or 1-3 or 1,4,5.")
    pages = [p for p in dict.fromkeys(pages) if 1 <= p <= total]
    if not pages:
        raise OfficeError(f"no such page: the file has {total} page(s).")
    return pages[:max_pages]


# ---------------------------------------------------------------------------------------------- LibreOffice
_SOFFICE_LOCK = threading.Lock()
_WIN_SOFFICE = ("C:/Program Files/LibreOffice/program/soffice.exe", "C:/Program Files (x86)/LibreOffice/program/soffice.exe")


def find_soffice() -> Optional[str]:
    """LUCY_SOFFICE, PATH, then the usual install folders. None = LibreOffice is not installed."""
    env = os.environ.get("LUCY_SOFFICE", "").strip().strip('"')
    if env and Path(env).is_file():
        return env
    for name in ("soffice", "libreoffice", "soffice.exe"):
        p = shutil.which(name)
        if p:
            return p
    for p in _WIN_SOFFICE + ("/Applications/LibreOffice.app/Contents/MacOS/soffice",):
        if Path(p).is_file():
            return p
    return None


def soffice_convert(src: Path, fmt: str, out_dir: Path, timeout: int = 120) -> Path:
    """Convert with headless LibreOffice (one at a time; own profile folder so it never fights a running LibreOffice).
    fmt: 'pdf', 'xlsx', 'docx', ... -> the converted file."""
    exe = find_soffice()
    if not exe:
        raise OfficeError("LibreOffice is not installed, so this cannot be converted or rendered. Install LibreOffice (free) "
                          "or set LUCY_SOFFICE to soffice.exe. office_read still works for the text.")
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = _paths.RUNTIME / "tmp" / "lo_profile"
    profile.mkdir(parents=True, exist_ok=True)
    cmd = [exe, f"-env:UserInstallation={profile.resolve().as_uri()}", "--headless", "--norestore", "--convert-to", fmt,
           "--outdir", str(out_dir), str(src)]
    with _SOFFICE_LOCK:
        try:
            r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
                               creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired as e:
            raise OfficeError(f"LibreOffice took longer than {timeout}s on {src.name}. Try a smaller file.") from e
    out = out_dir / (src.stem + "." + fmt.split(":")[0])
    if not out.is_file():
        raise OfficeError(f"LibreOffice could not convert {src.name}: {(r.stderr or r.stdout or '').strip()[:200]}")
    return out


def render_pdf_pages(pdf: Path, pages: List[int], out_dir: Path, dpi: int = 80) -> List[Path]:
    """PDF -> PNG per page. pypdfium2 first (pure pip wheel, works on Windows), then pdftoppm."""
    out_dir.mkdir(parents=True, exist_ok=True)
    outs: List[Path] = []
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(pdf))
        try:
            for p in pages:
                if p > len(doc):
                    continue
                img = doc[p - 1].render(scale=dpi / 72.0).to_pil()
                f = out_dir / f"page-{p}.png"
                img.convert("RGB").save(f, "PNG")
                outs.append(f)
        finally:
            doc.close()
        return outs
    except ImportError:
        pass
    exe = shutil.which("pdftoppm")
    if not exe:
        raise OfficeError("cannot turn pages into pictures: install the 'pypdfium2' package (pip install pypdfium2).")
    for p in pages:
        base = out_dir / f"page-{p}"
        subprocess.run([exe, "-png", "-r", str(dpi), "-f", str(p), "-l", str(p), "-singlefile", str(pdf), str(base)],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=120, creationflags=_NO_WINDOW)
        if Path(str(base) + ".png").is_file():
            outs.append(Path(str(base) + ".png"))
    return outs


def pdf_page_count(pdf: Path) -> int:
    pypdf = need("pypdf", "pypdf")
    return len(pypdf.PdfReader(str(pdf)).pages)


def file_key(p: Path) -> str:
    st = p.stat()
    return hashlib.sha1(f"{p.as_posix()}|{st.st_size}|{int(st.st_mtime)}".encode("utf-8")).hexdigest()[:10]


# ---------------------------------------------------------------------------------------------- spreadsheet values
EXCEL_ERRORS = ("#REF!", "#DIV/0!", "#NAME?", "#VALUE!", "#N/A", "#NUM!", "#NULL!")


def recalculated(path: Path):
    """The workbook WITH computed values (openpyxl data_only), calculated by LibreOffice on a throw-away copy.
    None when LibreOffice is missing or fails: callers then show formulas without values."""
    if not find_soffice():
        return None
    openpyxl = need("openpyxl", "openpyxl")
    tmp = Path(tempfile.mkdtemp(prefix="lucy_calc_"))
    try:
        (tmp / "in").mkdir()
        src = tmp / "in" / ("book" + path.suffix.lower())
        shutil.copy2(path, src)
        out = soffice_convert(src, "xlsx", tmp / "out", timeout=90)
        return openpyxl.load_workbook(out, data_only=True)
    except Exception:                                    # noqa: BLE001
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def formula_errors(path: Path, limit: int = 6) -> List[str]:
    """['Sheet1!D5 shows #DIV/0! (=B5/C5)', ...] for formulas that calculate to an error. [] if none OR not checkable."""
    openpyxl = need("openpyxl", "openpyxl")
    try:
        formulas = openpyxl.load_workbook(path)
    except Exception:                                    # noqa: BLE001
        return []
    if not any(isinstance(c.value, str) and c.value.startswith("=") for ws in formulas.worksheets for row in ws.iter_rows() for c in row):
        return []
    values = recalculated(path)
    if values is None:
        return []
    out: List[str] = []
    for ws in formulas.worksheets:
        if ws.title not in values.sheetnames:
            continue
        vs = values[ws.title]
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith("="):
                    v = vs[c.coordinate].value
                    if isinstance(v, str) and (v in EXCEL_ERRORS or v.startswith("Err:")):
                        out.append(f"{ws.title}!{c.coordinate} shows {v} ({c.value[:50]})")
                        if len(out) >= limit:
                            return out
    return out
