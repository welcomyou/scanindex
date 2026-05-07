"""
PDF signing core.

WinCSPSigner: a pyHanko Signer backed by the Windows Certificate Store.
sign_single_pdf / batch_sign: the public API called from the UI.

Visible signature is rendered top-left of the chosen page (default page 1).
Box semantic: (left_margin, top_margin, width, height) in points, measured
from the page's top-left corner. The actual PDF coordinates (origin
bottom-left) are computed per-page from the MediaBox, so the stamp lands
correctly on portrait, landscape, A3, etc.
"""

import asyncio
import io
import math
import os
from datetime import datetime
from typing import List, Optional, Tuple, Callable

from asn1crypto import x509 as asn1_x509
from asn1crypto.algos import SignedDigestAlgorithm
from pyhanko_certvalidator.registry import SimpleCertificateStore

from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.sign import fields, signers
from pyhanko.sign.fields import SigFieldSpec
try:
    from pyhanko.sign.timestamps import HTTPTimeStamper
except ImportError:
    HTTPTimeStamper = None

try:
    from pyhanko.sign.signers.pdf_cms import Signer as _BaseSigner
except ImportError:
    from pyhanko.sign.signers import Signer as _BaseSigner   # older pyhanko

from pyhanko.stamp import BaseStamp, TextStamp, TextStampStyle
from pyhanko.pdf_utils import generic, layout
from pyhanko.pdf_utils.images import PdfImage
try:
    from pyhanko.pdf_utils.text import TextBoxStyle
    _HAS_TEXT_BOX_STYLE = True
except ImportError:
    _HAS_TEXT_BOX_STYLE = False
try:
    from PIL import Image, ImageDraw, ImageFont
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False
try:
    from tzlocal import get_localzone
except ImportError:
    get_localzone = None


# ── Rotation-aware stamp ────────────────────────────────────────────────────

class _CustomTextStamp(TextStamp):
    """TextStamp with two extras over the default:

      1. Horizontal scaling (Tz operator) injected after BT — this squeezes
         glyph advances to *h_scale*% so the text reads compact instead of airy.
      2. Optional 90/180/270° rotation baked into the content stream as a
         `cm` wrap (chosen over /Matrix because some browser PDF viewers
         ignore /Matrix on signature widget appearances).

    For rotation: pass `page_rotation` ≠ 0 along with `real_width` / `real_height`
    matching the actual /Rect dimensions on the MediaBox.
    """

    def __init__(self, *, writer, style, box, text_params,
                 page_rotation: int = 0,
                 real_width: float = None, real_height: float = None,
                 h_scale: int = 90):
        super().__init__(writer=writer, style=style, box=box,
                         text_params=text_params)
        self._page_rotation = page_rotation % 360
        self._real_width  = real_width  if real_width  is not None else (box.width  if box else 0)
        self._real_height = real_height if real_height is not None else (box.height if box else 0)
        self._h_scale = h_scale

    def get_default_text_params(self):
        return _timestamp_params(self.style.timestamp_format)

    def as_form_xobject(self):
        xobj = super().as_form_xobject()

        # 1) Tighten glyph advances horizontally
        if self._h_scale and self._h_scale != 100:
            xobj._data = xobj.data.replace(
                b'BT ', b'BT %d Tz ' % self._h_scale, 1
            )

        # 2) Bake page-rotation compensation into the stream
        rot = self._page_rotation
        if rot in (90, 180, 270):
            rw, rh = float(self._real_width), float(self._real_height)
            if rot == 90:
                cm = (0, 1, -1, 0, rw, 0)
            elif rot == 180:
                cm = (-1, 0, 0, -1, rw, rh)
            else:  # 270
                cm = (0, -1, 1, 0, 0, rh)
            xobj._data = (
                b'q %g %g %g %g %g %g cm ' % cm
                + xobj.data
                + b' Q'
            )
            xobj[generic.pdf_name("/BBox")] = generic.ArrayObject([
                generic.FloatObject(0.0), generic.FloatObject(rh),
                generic.FloatObject(rw), generic.FloatObject(0.0),
            ])

        try:
            del xobj['/Length']   # force pyhanko to recompute on serialise
        except KeyError:
            pass
        return xobj


# Backwards-compat alias used elsewhere in the module
_RotatedTextStamp = _CustomTextStamp


from dataclasses import dataclass

@dataclass(frozen=True)
class _CustomTextStampStyle(TextStampStyle):
    """TextStampStyle that always emits a _CustomTextStamp — applies
    horizontal compression and (if page_rotation ≠ 0) rotation."""

    page_rotation: int = 0
    h_scale: int = 90

    def create_stamp(self, writer, box, text_params):
        rot = self.page_rotation % 360
        if rot in (90, 270) and box is not None:
            real_w, real_h = box.width, box.height
            natural_box = layout.BoxConstraints(width=real_h, height=real_w)
            return _CustomTextStamp(
                writer=writer, style=self, box=natural_box,
                text_params=text_params,
                page_rotation=rot, real_width=real_w, real_height=real_h,
                h_scale=self.h_scale,
            )
        return _CustomTextStamp(
            writer=writer, style=self, box=box,
            text_params=text_params,
            page_rotation=rot,
            real_width=(box.width if box else 0),
            real_height=(box.height if box else 0),
            h_scale=self.h_scale,
        )


# Backwards-compat alias
_RotatedTextStampStyle = _CustomTextStampStyle

from scanindex.core.pdf.win_cert_store import sign_data


DEFAULT_STAMP_TEMPLATE = "Xác nhận sao tại kho lưu trữ\n{unit_org}"
DEFAULT_TSA_URL = "http://tsa.ca.gov.vn"
STAMP_TEMPLATE_FIELDS = (
    "cn", "org", "ou", "unit_org", "subject", "issuer", "serial",
    "not_after", "ts", "datetime", "date", "time", "reason", "location",
)


def _unique_nonempty(values: List[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        value = str(value or "").strip()
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _find_windows_font_path() -> Optional[str]:
    candidates = [
        r"C:\Windows\Fonts\times.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\tahoma.ttf",
        r"C:\Windows\Fonts\calibri.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _timestamp_params(fmt: str) -> dict:
    tz = get_localzone() if get_localzone is not None else None
    now = datetime.now(tz=tz)
    return {
        "ts": now.strftime(fmt),
        "datetime": now.strftime(fmt),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
    }


def _stamp_template_values(
    cert_info: dict,
    *,
    reason: Optional[str] = None,
    location: Optional[str] = None,
    ts: str = "%(ts)s",
    date: str = "%(date)s",
    time: str = "%(time)s",
) -> dict:
    org = str(cert_info.get("org") or "").strip()
    ou = str(cert_info.get("ou") or "").strip()
    cn = str(cert_info.get("cn") or "").strip()
    unit_org = ", ".join(_unique_nonempty([ou, org])) or org or ou or cn

    not_after = cert_info.get("not_after") or ""
    if hasattr(not_after, "strftime"):
        not_after = not_after.strftime("%Y-%m-%d")
    else:
        not_after = str(not_after)

    return {
        "cn": cn,
        "org": org,
        "ou": ou,
        "unit_org": unit_org,
        "subject": str(cert_info.get("subject") or ""),
        "issuer": str(cert_info.get("issuer") or ""),
        "serial": str(cert_info.get("serial") or ""),
        "not_after": not_after,
        "ts": ts,
        "datetime": ts,
        "date": date,
        "time": time,
        "reason": str(reason or ""),
        "location": str(location or ""),
    }


def render_stamp_template(
    cert_info: dict,
    stamp_template: Optional[str] = None,
    *,
    reason: Optional[str] = None,
    location: Optional[str] = None,
    ts: str = "%(ts)s",
    date: str = "%(date)s",
    time: str = "%(time)s",
) -> str:
    template = (stamp_template or DEFAULT_STAMP_TEMPLATE).strip()
    values = _stamp_template_values(
        cert_info, reason=reason, location=location, ts=ts, date=date, time=time
    )
    return template.format(**values).strip()


def _render_text_image(
    lines: List[str],
    box_w_pt: float,
    box_h_pt: float,
    *,
    font_size_pt: float,
    font_path: Optional[str],
    compact_scale: float = 0.86,
    padding_pt: float = 2.0,
    dpi_scale: int = 4,
) -> Image.Image:
    """Render the visible signature as pixels, with tight fitting.

    pyHanko's TextBox routes OpenType fonts through HarfBuzz and can produce
    very loose-looking advances in some Vietnamese signing appearances.
    Rendering to a bitmap gives us deterministic spacing while keeping
    pyHanko for CMS/PAdES.
    """
    if not _HAS_PIL:
        raise RuntimeError("Pillow is required for bitmap signature appearance")

    width_px = max(1, int(round(box_w_pt * dpi_scale)))
    height_px = max(1, int(round(box_h_pt * dpi_scale)))
    pad_px = max(0, int(round(padding_pt * dpi_scale)))
    avail_w = max(1, width_px - 2 * pad_px)
    avail_h = max(1, height_px - 2 * pad_px)

    def load_font(size_px: int):
        if font_path:
            try:
                return ImageFont.truetype(font_path, size=size_px)
            except Exception:
                pass
        return ImageFont.load_default()

    def line_size(font, text: str) -> Tuple[int, int]:
        probe = Image.new("L", (1, 1), 0)
        draw = ImageDraw.Draw(probe)
        bbox = draw.textbbox((0, 0), text, font=font)
        return max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])

    # Re-fit in pixels because Pillow and PDF font metrics are not identical.
    requested_px = max(1, int(round(font_size_pt * dpi_scale)))
    size_px = requested_px
    while size_px > 1:
        font = load_font(size_px)
        widths_heights = [line_size(font, line) for line in lines]
        line_gap = max(1, int(round(size_px * 0.15)))
        total_h = sum(h + 4 for _, h in widths_heights) + line_gap * max(0, len(lines) - 1)
        max_w = max((w + 4 for w, _ in widths_heights), default=1)
        if max_w * compact_scale <= avail_w and total_h <= avail_h:
            break
        size_px -= 1

    font = load_font(size_px)
    line_gap = max(1, int(round(size_px * 0.15)))
    line_images = []
    for line in lines:
        w, h = line_size(font, line)
        raw = Image.new("RGBA", (w + 4, h + 4), (255, 255, 255, 0))
        draw = ImageDraw.Draw(raw)
        bbox = draw.textbbox((0, 0), line, font=font)
        draw.text((2 - bbox[0], 2 - bbox[1]), line, font=font, fill=(0, 0, 0, 255))
        target_w = max(1, min(avail_w, int(round(raw.width * compact_scale))))
        if target_w != raw.width:
            raw = raw.resize((target_w, raw.height), Image.Resampling.LANCZOS)
        line_images.append(raw)

    img = Image.new("RGBA", (width_px, height_px), (255, 255, 255, 0))
    y = pad_px
    for line_img in line_images:
        if y + line_img.height > height_px - pad_px:
            break
        img.alpha_composite(line_img, (pad_px, y))
        y += line_img.height + line_gap
    return img


def _measure_text_image_box(
    lines: List[str],
    *,
    font_size_pt: float,
    font_path: Optional[str],
    compact_scale: float = 0.86,
    padding_pt: float = 2.0,
    dpi_scale: int = 4,
) -> Tuple[int, int]:
    """Return the natural box size for the bitmap stamp renderer."""
    if not _HAS_PIL:
        raise RuntimeError("Pillow is required for bitmap signature measurement")

    def load_font(size_px: int):
        if font_path:
            try:
                return ImageFont.truetype(font_path, size=size_px)
            except Exception:
                pass
        return ImageFont.load_default()

    def line_size(font, text: str) -> Tuple[int, int]:
        probe = Image.new("L", (1, 1), 0)
        draw = ImageDraw.Draw(probe)
        bbox = draw.textbbox((0, 0), text, font=font)
        return max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])

    lines = lines or [""]
    size_px = max(1, int(round(font_size_pt * dpi_scale)))
    font = load_font(size_px)
    line_gap = max(1, int(round(size_px * 0.15)))
    pad_px = max(0, int(round(padding_pt * dpi_scale)))
    raw_sizes = [(w + 4, h + 4) for w, h in (line_size(font, line) for line in lines)]

    width_px = 2 * pad_px + max((w for w, _ in raw_sizes), default=1) * compact_scale
    height_px = (
        2 * pad_px
        + sum(h for _, h in raw_sizes)
        + line_gap * max(0, len(raw_sizes) - 1)
    )
    return max(1, math.ceil(width_px / dpi_scale)), max(1, math.ceil(height_px / dpi_scale))


class _ImageTextStamp(BaseStamp):
    def __init__(self, *, writer, style, box, text_params,
                 page_rotation: int = 0,
                 real_width: float = None, real_height: float = None):
        super().__init__(writer=writer, style=style, box=box)
        self.text_params = text_params or {}
        self._page_rotation = page_rotation % 360
        self._real_width = real_width if real_width is not None else (box.width if box else 0)
        self._real_height = real_height if real_height is not None else (box.height if box else 0)

    def _stamp_text(self) -> str:
        params = _timestamp_params(self.style.timestamp_format)
        params.update(self.text_params)
        return self.style.stamp_text % params

    def _render_inner_content(self):
        text = self._stamp_text()
        lines = text.splitlines()
        img = _render_text_image(
            lines,
            self.box.width,
            self.box.height,
            font_size_pt=self.style.image_font_size,
            font_path=self.style.image_font_path,
            compact_scale=self.style.compact_scale,
            padding_pt=self.style.image_padding,
        )
        content = PdfImage(
            img,
            writer=self.writer,
            box=layout.BoxConstraints(width=self.box.width, height=self.box.height),
        )
        rendered = content.render()
        self.import_resources(content.resources)
        return [rendered]

    def as_form_xobject(self):
        xobj = super().as_form_xobject()

        rot = self._page_rotation
        if rot in (90, 180, 270):
            rw, rh = float(self._real_width), float(self._real_height)
            if rot == 90:
                cm = (0, 1, -1, 0, rw, 0)
            elif rot == 180:
                cm = (-1, 0, 0, -1, rw, rh)
            else:
                cm = (0, -1, 1, 0, 0, rh)
            xobj._data = (
                b'q %g %g %g %g %g %g cm ' % cm
                + xobj.data
                + b' Q'
            )
            xobj[generic.pdf_name("/BBox")] = generic.ArrayObject([
                generic.FloatObject(0.0), generic.FloatObject(rh),
                generic.FloatObject(rw), generic.FloatObject(0.0),
            ])

        try:
            del xobj['/Length']
        except KeyError:
            pass
        return xobj


@dataclass(frozen=True)
class _ImageTextStampStyle(TextStampStyle):
    page_rotation: int = 0
    image_font_path: Optional[str] = None
    image_font_size: float = 10.0
    compact_scale: float = 0.86
    image_padding: float = 2.0

    def create_stamp(self, writer, box, text_params):
        rot = self.page_rotation % 360
        if rot in (90, 270) and box is not None:
            real_w, real_h = box.width, box.height
            natural_box = layout.BoxConstraints(width=real_h, height=real_w)
            return _ImageTextStamp(
                writer=writer, style=self, box=natural_box,
                text_params=text_params,
                page_rotation=rot, real_width=real_w, real_height=real_h,
            )
        return _ImageTextStamp(
            writer=writer, style=self, box=box, text_params=text_params,
            page_rotation=rot,
            real_width=(box.width if box else 0),
            real_height=(box.height if box else 0),
        )

# Default box: 0,0 (top-left corner), 220×60 pt — gives ~10pt font for
# typical Vietnamese gov cert org names. User adjusts box, font auto-fits.
# (left_margin, top_margin, width, height)
SIG_BOX_DEFAULT: Tuple[float, float, float, float] = (0.0, 0.0, 220.0, 60.0)


def _apply_widget_rotation(writer, page_idx: int) -> None:
    """
    If the target page has /Rotate ≠ 0, set the signature widget's
    /MK <</R rot>> entry so its appearance is rotated to match the page —
    otherwise the stamp will display sideways.
    """
    from pyhanko.pdf_utils import generic
    page = writer.root["/Pages"]["/Kids"][page_idx]
    page = page.get_object() if hasattr(page, "get_object") else page

    rot = int(page.get("/Rotate", 0)) % 360
    if rot == 0:
        return

    annots = page.get("/Annots")
    if annots is None:
        return
    annots_obj = annots.get_object() if hasattr(annots, "get_object") else annots

    for ref in annots_obj:
        widget = ref.get_object() if hasattr(ref, "get_object") else ref
        if widget.get("/Subtype") == "/Widget" and widget.get("/FT") == "/Sig":
            widget["/MK"] = generic.DictionaryObject({
                generic.pdf_name("/R"): generic.NumberObject(rot),
            })
            try:
                writer.update_container(widget)
            except Exception:
                pass


def _resolve_box_for_page(writer, page_idx: int, box_tl: Tuple[float, float, float, float]
                          ) -> Tuple[float, float, float, float]:
    """
    Convert a (left, top, width, height) box — measured from the top-left
    corner of the page AS DISPLAYED — into a PDF-coordinate (x1, y1, x2, y2)
    rect on the underlying MediaBox.

    Handles /Rotate = 0, 90, 180, 270 so the stamp lands at the visual
    top-left regardless of how the page is encoded.
    """
    pages = writer.root["/Pages"]["/Kids"]
    page = pages[page_idx]
    page = page.get_object() if hasattr(page, "get_object") else page

    mb = [float(v) for v in page["/MediaBox"]]
    W = mb[2] - mb[0]                       # MediaBox width in PDF coords
    H = mb[3] - mb[1]                       # MediaBox height
    rot = int(page.get("/Rotate", 0)) % 360

    left, top, width, height = box_tl
    # 4 corners in display coords (y-down, origin = displayed top-left)
    corners_display = [
        (left,         top),
        (left + width, top),
        (left,         top + height),
        (left + width, top + height),
    ]

    def to_pdf(xd: float, yd: float) -> Tuple[float, float]:
        if rot == 0:
            return (mb[0] + xd,     mb[1] + H - yd)
        if rot == 90:
            return (mb[0] + yd,     mb[1] + xd)
        if rot == 180:
            return (mb[0] + W - xd, mb[1] + yd)
        if rot == 270:
            return (mb[0] + W - yd, mb[1] + H - xd)
        # Unknown rotation: treat as 0
        return (mb[0] + xd,     mb[1] + H - yd)

    pdf_pts = [to_pdf(*c) for c in corners_display]
    xs = [p[0] for p in pdf_pts]
    ys = [p[1] for p in pdf_pts]
    return (min(xs), min(ys), max(xs), max(ys))


# ── Custom Signer ─────────────────────────────────────────────────────────────

class WinCSPSigner(_BaseSigner):
    """
    pyHanko Signer that delegates raw signing to the Windows CSP/CNG stack.

    PIN prompts are shown by the token driver — we never touch the PIN.
    Works with BKAV, CA2, Viettel, FPT, VNPT tokens and software certs.
    """

    def __init__(self, cert_info: dict, digest_algorithm: str = "sha256"):
        self._cert_info = cert_info
        cert = asn1_x509.Certificate.load(cert_info["der"])

        # Determine signature mechanism from public-key algorithm.
        # Windows CSP/CNG with PKCS#1 v1.5 padding → "<digest>_rsa" or "_ecdsa".
        pubkey_algo = cert.public_key.algorithm   # 'rsa' | 'ec' | 'dsa'
        if pubkey_algo == "rsa":
            mech_oid = f"{digest_algorithm}_rsa"
        elif pubkey_algo == "ec":
            mech_oid = f"{digest_algorithm}_ecdsa"
        else:
            raise NotImplementedError(f"Unsupported key algorithm: {pubkey_algo}")

        super().__init__(
            signing_cert=cert,
            cert_registry=SimpleCertificateStore(),
            signature_mechanism=SignedDigestAlgorithm({"algorithm": mech_oid}),
        )

    # ── pyHanko Signer interface ──────────────────────────────────────────

    async def async_sign_raw(
        self, data: bytes, digest_algorithm: str, dry_run: bool = False
    ) -> bytes:
        if dry_run:
            # pyhanko uses this to reserve space — return a plausible-sized buffer.
            # RSA-2048 → 256 bytes; RSA-4096 → 512.
            key_size = self.signing_cert.public_key.bit_size // 8
            return b"\x00" * key_size

        loop = asyncio.get_event_loop()
        # Run blocking ctypes call in a thread pool so we don't block the loop
        return await loop.run_in_executor(
            None, lambda: sign_data(self._cert_info, data, digest_algorithm)
        )


# ── Stamp appearance ──────────────────────────────────────────────────────────

def _build_stamp_lines(
    cert_info: dict,
    stamp_template: Optional[str] = None,
    *,
    reason: Optional[str] = None,
    location: Optional[str] = None,
    ts: str = "%(ts)s",
    date: str = "%(date)s",
    time: str = "%(time)s",
) -> List[str]:
    """Build the visible stamp lines from the configurable template."""
    text = render_stamp_template(
        cert_info,
        stamp_template,
        reason=reason,
        location=location,
        ts=ts,
        date=date,
        time=time,
    )
    return text.splitlines() or [""]


def _build_stamp_style(cert_info: dict, font_size: float = 10.0,
                       page_rotation: int = 0,
                       stamp_template: Optional[str] = None,
                       reason: Optional[str] = None,
                       location: Optional[str] = None) -> TextStampStyle:
    """
    Build a TextStampStyle showing Ký bởi / Cơ quan / Đơn vị / Thời gian.

    font_size may be fractional (auto-computed by compute_fit_font_size).
    If page_rotation ≠ 0, returns a rotated style that compensates for
    the page's /Rotate so the stamp displays upright.
    """
    stamp_text = "\n".join(_build_stamp_lines(
        cert_info, stamp_template, reason=reason, location=location,
    ))
    # font_size for the engine factory must be int — round but cap at 1+
    fs_int = max(1, int(round(font_size)))
    font_factory = _load_font_factory(fs_int)
    font_path = _find_windows_font_path()

    common_kwargs = dict(
        stamp_text=stamp_text,
        background_opacity=0.0,
        border_width=0,
        timestamp_format="%Y-%m-%dT%H:%M:%S%z",
    )

    if font_factory is not None and _HAS_TEXT_BOX_STYLE:
        layout_rule = layout.SimpleBoxLayoutRule(
            x_align=layout.AxisAlignment.ALIGN_MIN,
            y_align=layout.AxisAlignment.ALIGN_MIN,   # top
            inner_content_scaling=layout.InnerScaling.SHRINK_TO_FIT,
            margins=layout.Margins.uniform(2),
        )
        common_kwargs["text_box_style"] = TextBoxStyle(
            font=font_factory,
            font_size=fs_int,
            leading=max(fs_int + 1, int(round(fs_int * 1.15))),
            box_layout_rule=layout_rule,
        )

    # h_scale=100 = NO horizontal compression. Compressing past ~95% with
    # Vietnamese-extended glyphs (ở, ự, ắ…) often breaks combining-mark
    # positioning in some font/shaper combos, so we leave it at 100.
    if _HAS_PIL:
        return _ImageTextStampStyle(
            page_rotation=page_rotation,
            image_font_path=font_path,
            image_font_size=font_size,
            compact_scale=0.86,
            image_padding=2.0,
            **common_kwargs,
        )

    return _CustomTextStampStyle(page_rotation=page_rotation, h_scale=100,
                                 **common_kwargs)


def compute_stamp_natural_size(cert_info: dict, font_size: int = 10,
                               h_scale: int = 100,
                               stamp_template: Optional[str] = None,
                               reason: Optional[str] = None,
                               location: Optional[str] = None) -> Tuple[int, int]:
    """Legacy helper: returns natural (width, height) of stamp text at the
    given font size. Kept for the old "Vừa text" button (sets box to fit
    text). For the standard fixed-box auto-fit behaviour, use
    :func:`compute_fit_font_size` instead."""
    factory = _load_font_factory(font_size)
    if factory is None:
        return (200, 50)
    try:
        from pyhanko.pdf_utils.writer import PdfFileWriter
        writer = PdfFileWriter()
        engine = factory.create_font_engine(writer)
    except Exception:
        return (200, 50)

    sample_ts = "0000-00-00T00:00:00+0700"
    text_lines = _build_stamp_lines(
        cert_info,
        stamp_template,
        reason=reason,
        location=location,
        ts=sample_ts,
        date="0000-00-00",
        time="00:00:00",
    )

    if _HAS_PIL:
        try:
            return _measure_text_image_box(
                text_lines,
                font_size_pt=float(font_size),
                font_path=_find_windows_font_path(),
                compact_scale=0.86,
                padding_pt=2.0,
            )
        except Exception:
            pass

    max_w_pt = 0.0
    for line in text_lines:
        try:
            r = engine.shape(line)
            w = r.x_advance * font_size
            if w > max_w_pt:
                max_w_pt = w
        except Exception:
            continue

    rendered_w = max_w_pt * (h_scale / 100.0)
    height_pt = len(text_lines) * (font_size + 1)
    return (int(rendered_w) + 4, int(height_pt) + 4)


def compute_fit_font_size(cert_info: dict, box_w: float, box_h: float,
                          max_size: float = 14.0, min_size: float = 5.0,
                          compact_scale: float = 0.86,
                          stamp_template: Optional[str] = None,
                          reason: Optional[str] = None,
                          location: Optional[str] = None,
                          ) -> float:
    """
    Fixed-box auto-fit: given a fixed signature box, compute the
    largest font size such that all stamp lines fit within the box.

    The user picks the rect, and the text scales to fill it.

    Returns a float pt size, clamped to [min_size, max_size].
    """
    factory = _load_font_factory(int(max_size))
    if factory is None:
        return 9.0

    try:
        from pyhanko.pdf_utils.writer import PdfFileWriter
        writer = PdfFileWriter()
        engine = factory.create_font_engine(writer)
    except Exception:
        return 9.0

    sample_ts = "0000-00-00T00:00:00+0700"
    text_lines = _build_stamp_lines(
        cert_info,
        stamp_template,
        reason=reason,
        location=location,
        ts=sample_ts,
        date="0000-00-00",
        time="00:00:00",
    )
    if not text_lines:
        return 9.0

    # x_advance returned by harfbuzz is in font-em units already scaled so
    # that `width_pt = x_advance * font_size`. So at font_size=1, the
    # rendered width equals the raw x_advance value.
    max_unit_w = 0.0
    for line in text_lines:
        try:
            r = engine.shape(line)
            if r.x_advance > max_unit_w:
                max_unit_w = r.x_advance
        except Exception:
            continue
    if max_unit_w <= 0:
        return 9.0

    # Inner padding (left+right, top+bottom) — keep some breathing room.
    pad_x = 4.0
    pad_y = 4.0
    leading_factor = 1.15        # leading ≈ 1.15 × font_size (typical)
    n = len(text_lines)

    avail_w = max(box_w - pad_x, 1.0)
    avail_h = max(box_h - pad_y, 1.0)

    size_by_w = avail_w / max(max_unit_w * compact_scale, 0.1)
    size_by_h = avail_h / (n * leading_factor)

    fit = min(size_by_w, size_by_h)
    fit = max(min_size, min(max_size, fit))
    return round(fit, 1)


def _load_font_factory(font_size: int):
    """Return a FontEngineFactory with FULL Vietnamese support.

    Times New Roman first: it reads dense and formal while supporting
    Vietnamese text well on standard Windows installations.
    """
    candidates = [
        r"C:\Windows\Fonts\times.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\tahoma.ttf",
        r"C:\Windows\Fonts\calibri.ttf",
    ]
    try:
        from pyhanko.pdf_utils.font.opentype import GlyphAccumulatorFactory
    except ImportError:
        return None
    for path in candidates:
        if os.path.exists(path):
            try:
                return GlyphAccumulatorFactory(font_file=path, font_size=font_size)
            except Exception:
                continue
    return None


def _build_timestamper(tsa_url: Optional[str]):
    url = str(tsa_url or "").strip()
    if not url:
        return None
    if HTTPTimeStamper is None:
        raise RuntimeError("pyHanko timestamp support is not available")
    return HTTPTimeStamper(url, timeout=10)


# ── Core signing functions ────────────────────────────────────────────────────

def sign_single_pdf(
    input_path: str,
    output_path: str,
    cert_info: dict,
    sig_box: Tuple[float, float, float, float] = SIG_BOX_DEFAULT,
    page: int = 0,
    reason: Optional[str] = None,
    location: Optional[str] = None,
    stamp_template: Optional[str] = None,
    tsa_url: Optional[str] = None,
) -> None:
    """Sign one PDF file, writing to *output_path*."""

    signer = WinCSPSigner(cert_info)
    timestamper = _build_timestamper(tsa_url)

    meta = signers.PdfSignatureMetadata(
        field_name="Sig1",
        md_algorithm="sha256",
        reason=reason or None,
        location=location or None,
        certify=False,
    )

    # Fixed-box auto-fit: the user fixes the box, we scale the
    # font down to make all 4 stamp lines fit inside.
    box_w, box_h = sig_box[2], sig_box[3]
    fit_font_size = compute_fit_font_size(
        cert_info, box_w, box_h,
        stamp_template=stamp_template, reason=reason, location=location,
    )

    with open(input_path, "rb") as inf:
        writer = IncrementalPdfFileWriter(inf, strict=False)

        # Detect page rotation so the stamp can compensate for it
        page_obj = writer.root["/Pages"]["/Kids"][page]
        page_obj = page_obj.get_object() if hasattr(page_obj, "get_object") else page_obj
        page_rot = int(page_obj.get("/Rotate", 0)) % 360

        stamp_style = _build_stamp_style(
            cert_info,
            font_size=fit_font_size,
            page_rotation=page_rot,
            stamp_template=stamp_template,
            reason=reason,
            location=location,
        )

        # Convert (left, top, w, h) margin-from-top-left into PDF coords for THIS page
        pdf_box = _resolve_box_for_page(writer, page, sig_box)

        # Add the visible signature field at the requested position
        fields.append_signature_field(
            writer,
            SigFieldSpec(
                sig_field_name="Sig1",
                on_page=page,
                box=pdf_box,
            ),
        )

        pdf_signer = signers.PdfSigner(
            meta,
            signer=signer,
            timestamper=timestamper,
            stamp_style=stamp_style,
        )

        buf = io.BytesIO()

        # async_sign_pdf needs a running event loop; asyncio.run() creates one.
        # bytes_reserved is set explicitly because pyhanko's auto-estimator
        # makes a dry run that can't always accommodate custom signers.
        asyncio.run(pdf_signer.async_sign_pdf(
            writer,
            output=buf,
            bytes_reserved=32768 if timestamper is not None else 16384,
        ))

    with open(output_path, "wb") as outf:
        outf.write(buf.getvalue())


def batch_sign(
    input_paths: List[str],
    output_dir: str,
    cert_info: dict,
    sig_box: Tuple[float, float, float, float] = SIG_BOX_DEFAULT,
    page: int = 0,
    reason: Optional[str] = None,
    location: Optional[str] = None,
    stamp_template: Optional[str] = None,
    progress_cb: Optional[Callable[[int, int, Tuple[str, bool, str]], None]] = None,
    tsa_url: Optional[str] = None,
) -> List[Tuple[str, bool, str]]:
    """
    Sign all PDFs in *input_paths*, writing results to *output_dir*.

    Returns list of (input_path, success, error_message).
    progress_cb(done, total, last_result) is called after each file.
    """
    os.makedirs(output_dir, exist_ok=True)
    results: List[Tuple[str, bool, str]] = []
    total = len(input_paths)

    for i, src in enumerate(input_paths):
        fname = os.path.basename(src)
        dst = os.path.join(output_dir, fname)

        # Avoid silently overwriting the source file
        if os.path.abspath(src) == os.path.abspath(dst):
            base, ext = os.path.splitext(fname)
            dst = os.path.join(output_dir, f"{base}_signed{ext}")

        try:
            sign_single_pdf(
                src, dst, cert_info, sig_box, page, reason, location,
                stamp_template=stamp_template,
                tsa_url=tsa_url,
            )
            entry = (src, True, "")
        except Exception as exc:
            entry = (src, False, str(exc))

        results.append(entry)
        if progress_cb:
            progress_cb(i + 1, total, entry)

    return results
