"""
screen_ai_ocr.py - Direct DLL injection to Chrome's ScreenAI OCR library
=========================================================================
Bypasses Chrome browser entirely. Loads chrome_screen_ai.dll and calls
the OCR pipeline directly via ctypes.

Research based on Chromium source:
  https://chromium.googlesource.com/chromium/src/+/HEAD/services/screen_ai/

DLL Version: 140.14 (exported 107 symbols including TFLite C API)

Architecture:
  1. SetFileContentFunctions() - register callbacks for model file I/O
  2. InitOCRUsingCallback()    - initialize OCR pipeline (loads models via callbacks)
  3. PerformOCR(SkBitmap&)     - run OCR on image -> serialized VisualAnnotation protobuf
  4. FreeLibraryAllocatedCharArray() - free result buffer
  5. UninitializeOCR()         - cleanup

The critical challenge: PerformOCR takes const SkBitmap& (Skia C++ class).
We reconstruct the 56-byte binary layout of SkBitmap in ctypes.

Author: Reverse-engineered from Chromium/Skia source code
"""

import ctypes
import os
import sys
import struct
import logging
from pathlib import Path
from contextlib import contextmanager

# Platform-specific imports
if sys.platform == "win32":
    import ctypes.wintypes

logger = logging.getLogger(__name__)


def _console_safe_text(text: str) -> str:
    """Return text that Python's default console logger can always emit.

    ScreenAI occasionally logs complex-script glyphs. On Windows consoles whose
    active code page cannot encode those glyphs, the logging module prints
    "--- Logging error ---" even though OCR itself is fine.
    """
    value = str(text or "")
    encoding = getattr(sys.stderr, "encoding", None) or getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return value.encode(encoding, errors="backslashreplace").decode(encoding, errors="replace")
    except Exception:
        return value.encode("ascii", errors="backslashreplace").decode("ascii", errors="replace")


@contextmanager
def _suppress_native_stderr():
    """Suppress C/C++ stderr output (DLL internal logging like I0000, INFO:)."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        old_stderr = os.dup(2)
        os.dup2(devnull, 2)
        os.close(devnull)
        yield
    except Exception:
        yield
    else:
        os.dup2(old_stderr, 2)
        os.close(old_stderr)

# ===========================================================================
# SkBitmap Memory Layout (Chrome 140 / Skia m127+, x86_64 Windows MSVC)
# ===========================================================================
# Offset  Size  Field
# ------  ----  ----------------------------------------
#  0       8    fPixelRef.fPtr          (sk_sp<SkPixelRef> → pointer)
#  8       8    fPixmap.fPixels         (const void* → pixel data)
# 16       8    fPixmap.fRowBytes       (size_t)
# 24       8    fPixmap.fInfo.fColorInfo.fColorSpace.fPtr (sk_sp<SkColorSpace>)
# 32       4    fPixmap.fInfo.fColorInfo.fColorType       (int enum)
# 36       4    fPixmap.fInfo.fColorInfo.fAlphaType       (int enum)
# 40       4    fPixmap.fInfo.fDimensions.fWidth          (int32)
# 44       4    fPixmap.fInfo.fDimensions.fHeight         (int32)
# 48       8    fMips.fPtr              (sk_sp<SkMipmap> → pointer)
# ------  ----
# Total: 56 bytes

# SkColorType enum values
kUnknown_SkColorType = 0
kAlpha_8_SkColorType = 1
kRGB_565_SkColorType = 2
kARGB_4444_SkColorType = 3
kRGBA_8888_SkColorType = 4
kRGB_888x_SkColorType = 5
kBGRA_8888_SkColorType = 6  # = kN32 on Windows

# SkAlphaType enum values
kUnknown_SkAlphaType = 0
kOpaque_SkAlphaType = 1
kPremul_SkAlphaType = 2
kUnpremul_SkAlphaType = 3


class FakeSkPixelRef(ctypes.Structure):
    """
    Minimal fake SkPixelRef to prevent crash if DLL dereferences fPixelRef.

    Real layout (SkRefCntBase → SkRefCnt → SkPixelRef):
      Offset  Size  Field
       0       8    vtable pointer (virtual destructor)
       8       4    fRefCnt (atomic<int32_t>)
      12       4    padding
      16       4    fWidth
      20       4    fHeight
      24       8    fPixels (void*)
      32       8    fRowBytes (size_t)
      40       4    fTaggedGenID (atomic<uint32_t>)
      44+      ??   fGenIDChangeListeners (complex, zeroed)

    We allocate 128 bytes to be safe and zero-fill.
    vtable is set to NULL - will crash on virtual calls but
    most code paths don't call virtual methods on SkPixelRef.
    """
    _fields_ = [
        ("vtable_ptr", ctypes.c_void_p),      # 0: NULL (no virtual calls expected)
        ("fRefCnt", ctypes.c_int32),           # 8: ref count = 1
        ("_pad1", ctypes.c_int32),             # 12: padding
        ("fWidth", ctypes.c_int32),            # 16
        ("fHeight", ctypes.c_int32),           # 20
        ("fPixels", ctypes.c_void_p),          # 24: pixel data pointer
        ("fRowBytes", ctypes.c_size_t),        # 32
        ("fTaggedGenID", ctypes.c_uint32),     # 40
        ("_padding", ctypes.c_byte * 84),      # 44-127: zero-fill safety zone
    ]


class SkBitmap(ctypes.Structure):
    """
    Reconstructed SkBitmap (56 bytes, x86_64 MSVC).
    Matches Chrome 127-140+ Skia layout with fMips field.
    """
    _fields_ = [
        # sk_sp<SkPixelRef> fPixelRef
        ("fPixelRef_ptr", ctypes.c_void_p),       # offset 0,  8 bytes

        # SkPixmap fPixmap:
        #   const void* fPixels
        ("fPixmap_fPixels", ctypes.c_void_p),      # offset 8,  8 bytes
        #   size_t fRowBytes
        ("fPixmap_fRowBytes", ctypes.c_size_t),     # offset 16, 8 bytes
        #   SkImageInfo fInfo:
        #     SkColorInfo fColorInfo:
        #       sk_sp<SkColorSpace> fColorSpace
        ("fColorSpace_ptr", ctypes.c_void_p),       # offset 24, 8 bytes
        #       SkColorType fColorType
        ("fColorType", ctypes.c_int32),              # offset 32, 4 bytes
        #       SkAlphaType fAlphaType
        ("fAlphaType", ctypes.c_int32),              # offset 36, 4 bytes
        #     SkISize fDimensions:
        #       int32_t fWidth
        ("fWidth", ctypes.c_int32),                  # offset 40, 4 bytes
        #       int32_t fHeight
        ("fHeight", ctypes.c_int32),                 # offset 44, 4 bytes

        # sk_sp<SkMipmap> fMips
        ("fMips_ptr", ctypes.c_void_p),              # offset 48, 8 bytes
    ]

assert ctypes.sizeof(SkBitmap) == 56, f"SkBitmap size mismatch: {ctypes.sizeof(SkBitmap)} != 56"


# ===========================================================================
# Callback Types for SetFileContentFunctions
# ===========================================================================
# uint32_t (*get_file_content_size)(const char* relative_file_path)
GET_FILE_SIZE_FUNC = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_char_p)

# void (*get_file_content)(const char* relative_file_path, uint32_t buffer_size, char* buffer)
# CRITICAL: buffer must be c_void_p (raw pointer), NOT c_char_p (which ctypes treats as string)
GET_FILE_CONTENT_FUNC = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_void_p)

# void (*logger)(int severity, const char* message)
LOGGER_FUNC = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_char_p)


# ===========================================================================
# Minimal Protobuf Decoder (no dependency on google.protobuf)
# ===========================================================================
def decode_varint(data, pos):
    """Decode a varint from data at position pos. Returns (value, new_pos)."""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if (b & 0x80) == 0:
            return result, pos
        shift += 7
    raise ValueError("Truncated varint")


def decode_protobuf_fields(data):
    """
    Decode raw protobuf into list of (field_number, wire_type, value).
    wire_type 0 = varint, 1 = 64-bit, 2 = length-delimited, 5 = 32-bit
    """
    fields = []
    pos = 0
    while pos < len(data):
        tag, pos = decode_varint(data, pos)
        field_number = tag >> 3
        wire_type = tag & 0x07

        if wire_type == 0:  # varint
            value, pos = decode_varint(data, pos)
            fields.append((field_number, wire_type, value))
        elif wire_type == 1:  # 64-bit
            value = struct.unpack_from('<Q', data, pos)[0]
            pos += 8
            fields.append((field_number, wire_type, value))
        elif wire_type == 2:  # length-delimited
            length, pos = decode_varint(data, pos)
            value = data[pos:pos + length]
            pos += length
            fields.append((field_number, wire_type, value))
        elif wire_type == 5:  # 32-bit
            value = struct.unpack_from('<I', data, pos)[0]
            pos += 4
            fields.append((field_number, wire_type, value))
        else:
            break  # unknown wire type
    return fields


def parse_rect(data):
    """Parse Rect proto: x(1), y(2), width(3), height(4), angle(5 float)."""
    fields = decode_protobuf_fields(data)
    rect = {"x": 0, "y": 0, "width": 0, "height": 0, "angle": 0.0}
    for fn, wt, val in fields:
        if fn == 1: rect["x"] = val
        elif fn == 2: rect["y"] = val
        elif fn == 3: rect["width"] = val
        elif fn == 4: rect["height"] = val
        elif fn == 5 and wt == 5:
            rect["angle"] = struct.unpack('<f', struct.pack('<I', val))[0]
    return rect


def parse_symbol_box(data):
    """
    Parse SymbolBox proto:
      bounding_box(1), utf8_string(2), confidence(3)
    """
    fields = decode_protobuf_fields(data)
    result = {"bounding_box": None, "utf8_string": "", "confidence": 0.0}
    for fn, wt, val in fields:
        if fn == 1 and wt == 2: result["bounding_box"] = parse_rect(val)
        elif fn == 2 and wt == 2: result["utf8_string"] = val.decode("utf-8", errors="replace")
        elif fn == 3 and wt == 5:
            result["confidence"] = struct.unpack('<f', struct.pack('<I', val))[0]
    return result


# ContentType enum names
CONTENT_TYPE_NAMES = {
    0: "printed", 1: "handwritten", 2: "image", 3: "line_drawing",
    4: "separator", 5: "unreadable", 6: "formula", 7: "handwritten_formula",
    8: "signature",
}


def _unpack_rgb(packed_int):
    """Unpack leptonica-style packed RGB int32 → (r, g, b)."""
    # Leptonica packs as: (r << 24) | (g << 16) | (b << 8) | 0
    # But protobuf int32 is signed, so handle carefully
    v = packed_int & 0xFFFFFFFF
    r = (v >> 24) & 0xFF
    g = (v >> 16) & 0xFF
    b = (v >> 8) & 0xFF
    return (r, g, b)


def parse_word_box(data):
    """
    Parse WordBox proto - FULL schema:
      symbols(1), bounding_box(2), utf8_string(3), [dictionary_word(4) deprecated],
      language(5), has_space_after(6), estimate_color_success(7),
      foreground_gray(8), background_gray(9),
      foreground_rgb(10), background_rgb(11),
      direction(12), content_type(13), [orientation(14) deprecated],
      confidence(15), estimate_gray_success(16),
      whitespace_bounding_box(17)
    """
    fields = decode_protobuf_fields(data)
    word = {
        "symbols": [],
        "bounding_box": None,
        "utf8_string": "",
        "language": "",
        "has_space_after": False,
        "confidence": 0.0,
        "direction": 0,
        "content_type": 0,
        # Color estimation
        "estimate_color_success": False,
        "foreground_rgb": None,       # (r, g, b) tuple or None
        "background_rgb": None,       # (r, g, b) tuple or None
        "foreground_gray": 0,
        "background_gray": 0,
        # Whitespace
        "whitespace_bounding_box": None,
    }
    raw_fg_rgb = 0
    raw_bg_rgb = 0

    for fn, wt, val in fields:
        if fn == 1 and wt == 2: word["symbols"].append(parse_symbol_box(val))
        elif fn == 2 and wt == 2: word["bounding_box"] = parse_rect(val)
        elif fn == 3 and wt == 2: word["utf8_string"] = val.decode("utf-8", errors="replace")
        elif fn == 5 and wt == 2: word["language"] = val.decode("utf-8", errors="replace")
        elif fn == 6 and wt == 0: word["has_space_after"] = bool(val)
        elif fn == 7 and wt == 0: word["estimate_color_success"] = bool(val)
        elif fn == 8 and wt == 0: word["foreground_gray"] = val
        elif fn == 9 and wt == 0: word["background_gray"] = val
        elif fn == 10 and wt == 0: raw_fg_rgb = val
        elif fn == 11 and wt == 0: raw_bg_rgb = val
        elif fn == 12 and wt == 0: word["direction"] = val
        elif fn == 13 and wt == 0: word["content_type"] = val
        elif fn == 15 and wt == 5:
            word["confidence"] = struct.unpack('<f', struct.pack('<I', val))[0]
        elif fn == 17 and wt == 2: word["whitespace_bounding_box"] = parse_rect(val)

    # Unpack RGB values if color estimation succeeded
    if word["estimate_color_success"]:
        word["foreground_rgb"] = _unpack_rgb(raw_fg_rgb)
        word["background_rgb"] = _unpack_rgb(raw_bg_rgb)

    return word


def parse_line_box(data):
    """
    Parse LineBox proto - FULL schema:
      words(1), bounding_box(2), utf8_string(3), language(4),
      block_id(5), [order_within_block(6) deprecated],
      direction(7), content_type(8), [baseline_box(9) deprecated],
      confidence(10), paragraph_id(11)
    """
    fields = decode_protobuf_fields(data)
    line = {
        "words": [],
        "bounding_box": None,
        "utf8_string": "",
        "language": "",
        "block_id": 0,
        "paragraph_id": 0,
        "direction": 0,
        "content_type": 0,
        "confidence": 0.0,
    }
    for fn, wt, val in fields:
        if fn == 1 and wt == 2: line["words"].append(parse_word_box(val))
        elif fn == 2 and wt == 2: line["bounding_box"] = parse_rect(val)
        elif fn == 3 and wt == 2: line["utf8_string"] = val.decode("utf-8", errors="replace")
        elif fn == 4 and wt == 2: line["language"] = val.decode("utf-8", errors="replace")
        elif fn == 5 and wt == 0: line["block_id"] = val
        elif fn == 7 and wt == 0: line["direction"] = val
        elif fn == 8 and wt == 0: line["content_type"] = val
        elif fn == 10 and wt == 5:
            line["confidence"] = struct.unpack('<f', struct.pack('<I', val))[0]
        elif fn == 11 and wt == 0: line["paragraph_id"] = val
    return line


def parse_visual_annotation(data):
    """
    Parse VisualAnnotation proto:
      repeated UIComponent ui_component = 1;  (layout - unused for OCR)
      repeated LineBox lines = 2;              (OCR results)
    """
    fields = decode_protobuf_fields(data)
    lines = []
    for fn, wt, val in fields:
        if fn == 2 and wt == 2:
            lines.append(parse_line_box(val))
    return {"lines": lines}


# ===========================================================================
# ScreenAI OCR Engine
# ===========================================================================
class ScreenAIOCR:
    """
    Direct interface to chrome_screen_ai.dll for OCR without Chrome browser.

    Usage:
        ocr = ScreenAIOCR(
            dll_path="path/to/chrome_screen_ai.dll",
            model_dir="path/to/140.14/"
        )
        ocr.initialize()
        result = ocr.perform_ocr(image)  # PIL Image or numpy array
        ocr.shutdown()
    """

    def __init__(self, dll_path=None, model_dir=None):
        """
        Args:
            dll_path: Path to chrome_screen_ai.dll. Auto-detected if None.
            model_dir: Directory containing model files (where .tflite, .binarypb are).
                       Auto-detected if None.
        """
        if dll_path is None or model_dir is None:
            auto_dll, auto_model = self._auto_detect_paths()
            dll_path = dll_path or auto_dll
            model_dir = model_dir or auto_model

        self.dll_path = dll_path
        self.model_dir = model_dir
        self._dll = None
        self._dll_dir_handle = None
        self._initialized = False

        # File cache for model data (preloaded into memory)
        self._file_cache = {}

        # Keep references to ctypes callbacks to prevent GC
        self._get_file_size_cb = None
        self._get_file_content_cb = None
        self._logger_cb = None

        # Fake SkPixelRef (kept alive to prevent GC)
        self._fake_pixel_ref = None

    @staticmethod
    def _auto_detect_paths():
        """Auto-detect DLL and model paths relative to this script."""
        from scanindex.infra.paths import get_base_dir
        base = get_base_dir()
        screen_ai_dir = os.path.join(base, "models", "ocr_chrome_userdata_ori", "screen_ai")

        # Find latest version directory
        versions = []
        if os.path.exists(screen_ai_dir):
            for d in os.listdir(screen_ai_dir):
                full = os.path.join(screen_ai_dir, d)
                if os.path.isdir(full):
                    versions.append(full)

        if not versions:
            raise FileNotFoundError(f"No Screen AI versions found in {screen_ai_dir}")

        # Sort by version number (directory name)
        versions.sort(reverse=True)
        model_dir = versions[0]
        _LIB_NAMES = {
            "win32":  "chrome_screen_ai.dll",
            "darwin": "libchromescreenai.dylib",
            "linux":  "libchromescreenai.so",
        }
        lib_name = _LIB_NAMES.get(sys.platform, "chrome_screen_ai.dll")
        dll_path = os.path.join(model_dir, lib_name)

        if not os.path.exists(dll_path):
            raise FileNotFoundError(f"ScreenAI library not found: {dll_path}")

        return dll_path, model_dir

    def _preload_model_files(self):
        """
        Preload all model files into memory.
        The DLL calls our callbacks during InitOCRUsingCallback() to read model data.
        File paths requested are RELATIVE to the model directory.
        """
        logger.info(f"Preloading model files from: {self.model_dir}")

        # Read files_list_ocr.txt to know what files are needed
        file_list_path = os.path.join(self.model_dir, "files_list_ocr.txt")
        files_to_load = []

        if os.path.exists(file_list_path):
            with open(file_list_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        files_to_load.append(line)

        # Also load the engine config at root level
        engine_pb = "gocr_mobile_chrome_multiscript_2024_q4_engine.binarypb"
        if engine_pb not in files_to_load:
            files_to_load.insert(0, engine_pb)

        # Load all into cache
        loaded = 0
        for rel_path in files_to_load:
            # Normalize path separators
            norm_path = rel_path.replace("/", os.sep)
            full_path = os.path.join(self.model_dir, norm_path)

            if os.path.exists(full_path):
                with open(full_path, "rb") as f:
                    data = f.read()
                # Store with forward slashes as that's what the DLL will request
                self._file_cache[rel_path] = data
                loaded += 1
            else:
                logger.warning(f"Model file not found: {full_path}")

        logger.info(f"Preloaded {loaded}/{len(files_to_load)} model files "
                     f"({sum(len(v) for v in self._file_cache.values()) / 1024 / 1024:.1f} MB)")

    def _make_file_callbacks(self):
        """Create ctypes callback functions for SetFileContentFunctions."""

        @GET_FILE_SIZE_FUNC
        def get_file_size(relative_path_bytes):
            """Called by DLL to get file size. Returns 0 if not found."""
            try:
                rel_path = relative_path_bytes.decode("utf-8")
                logger.debug(f"[CB] get_file_size('{rel_path}')")

                # Try exact match first
                if rel_path in self._file_cache:
                    logger.debug(f"  -> {len(self._file_cache[rel_path])} bytes (exact)")
                    return len(self._file_cache[rel_path])

                # Try with normalized separators
                norm = rel_path.replace("\\", "/")
                if norm in self._file_cache:
                    logger.debug(f"  -> {len(self._file_cache[norm])} bytes (normalized)")
                    return len(self._file_cache[norm])

                # Try reading from disk as fallback
                full_path = os.path.join(self.model_dir, rel_path.replace("/", os.sep))
                if os.path.exists(full_path):
                    data = open(full_path, "rb").read()
                    self._file_cache[rel_path] = data
                    logger.debug(f"  -> Lazy-loaded: {rel_path} ({len(data)} bytes)")
                    return len(data)

                logger.warning(f"  -> NOT FOUND: {rel_path}")
                return 0
            except Exception as e:
                logger.error(f"get_file_size error: {e}")
                return 0

        @GET_FILE_CONTENT_FUNC
        def get_file_content(relative_path_bytes, buffer_size, buffer):
            """Called by DLL to read file content into buffer. buffer is c_void_p."""
            try:
                rel_path = relative_path_bytes.decode("utf-8")
                logger.debug(f"[CB] get_file_content('{rel_path}', buf_size={buffer_size}, buf_ptr={buffer})")

                data = self._file_cache.get(rel_path)
                if data is None:
                    norm = rel_path.replace("\\", "/")
                    data = self._file_cache.get(norm)

                if data is None:
                    logger.error(f"get_file_content: no data for {rel_path}")
                    return

                # Copy data to buffer
                size = min(len(data), buffer_size)
                ctypes.memmove(buffer, data, size)

            except Exception as e:
                logger.error(f"get_file_content error: {e}")

        # Store references to prevent GC
        self._get_file_size_cb = get_file_size
        self._get_file_content_cb = get_file_content

        return get_file_size, get_file_content

    def _make_logger_callback(self):
        """Create logger callback for SetLogger."""

        @LOGGER_FUNC
        def log_handler(severity, message_bytes):
            try:
                raw_msg = message_bytes.decode("utf-8", errors="replace") if message_bytes else ""
                msg = _console_safe_text(raw_msg)
                if severity <= 0:
                    logger.debug(f"[ScreenAI] {msg}")
                elif severity == 1:
                    logger.warning(f"[ScreenAI] {msg}")
                else:
                    logger.error(f"[ScreenAI] {msg}")
            except:
                pass

        self._logger_cb = log_handler
        return log_handler

    def _load_dll(self):
        """Load ScreenAI native library and resolve function pointers."""
        logger.info(f"Loading library: {self.dll_path}")

        dll_dir = os.path.dirname(self.dll_path)
        if sys.platform == "win32":
            self._dll_dir_handle = os.add_dll_directory(dll_dir)
            self._dll = ctypes.CDLL(self.dll_path)
        else:
            # macOS/Linux: set rpath-like env so dependent .so/.dylib can be found
            if sys.platform == "darwin":
                os.environ.setdefault("DYLD_LIBRARY_PATH", dll_dir)
            else:
                ld_path = os.environ.get("LD_LIBRARY_PATH", "")
                os.environ["LD_LIBRARY_PATH"] = f"{dll_dir}:{ld_path}" if ld_path else dll_dir
            self._dll = ctypes.CDLL(self.dll_path)

        # ---- Resolve all exported functions ----

        # GetLibraryVersion(uint32_t& major, uint32_t& minor)
        self._GetLibraryVersion = self._dll.GetLibraryVersion
        self._GetLibraryVersion.argtypes = [
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32)
        ]
        self._GetLibraryVersion.restype = None

        # EnableDebugMode()
        self._EnableDebugMode = self._dll.EnableDebugMode
        self._EnableDebugMode.argtypes = []
        self._EnableDebugMode.restype = None

        # SetFileContentFunctions(get_size_fn, get_content_fn)
        self._SetFileContentFunctions = self._dll.SetFileContentFunctions
        self._SetFileContentFunctions.argtypes = [GET_FILE_SIZE_FUNC, GET_FILE_CONTENT_FUNC]
        self._SetFileContentFunctions.restype = None

        # SetLogger(logger_fn) - may not exist on non-ChromeOS
        try:
            self._SetLogger = self._dll.SetLogger
            self._SetLogger.argtypes = [LOGGER_FUNC]
            self._SetLogger.restype = None
        except AttributeError:
            self._SetLogger = None

        # SetOCRLightMode(bool enabled)
        try:
            self._SetOCRLightMode = self._dll.SetOCRLightMode
            self._SetOCRLightMode.argtypes = [ctypes.c_bool]
            self._SetOCRLightMode.restype = None
        except AttributeError:
            self._SetOCRLightMode = None

        # GetMaxImageDimension() -> uint32_t
        try:
            self._GetMaxImageDimension = self._dll.GetMaxImageDimension
            self._GetMaxImageDimension.argtypes = []
            self._GetMaxImageDimension.restype = ctypes.c_uint32
        except AttributeError:
            self._GetMaxImageDimension = None

        # InitOCRUsingCallback() -> bool
        self._InitOCR = self._dll.InitOCRUsingCallback
        self._InitOCR.argtypes = []
        self._InitOCR.restype = ctypes.c_bool

        # PerformOCR(const SkBitmap&, uint32_t&) -> char*
        # C++ reference = pointer in ABI
        self._PerformOCR = self._dll.PerformOCR
        self._PerformOCR.argtypes = [
            ctypes.POINTER(SkBitmap),
            ctypes.POINTER(ctypes.c_uint32)
        ]
        self._PerformOCR.restype = ctypes.c_void_p  # char* (raw pointer)

        # FreeLibraryAllocatedCharArray(char*)
        self._FreeCharArray = self._dll.FreeLibraryAllocatedCharArray
        self._FreeCharArray.argtypes = [ctypes.c_void_p]
        self._FreeCharArray.restype = None

        # FreeLibraryAllocatedInt32Array(int32_t*)
        self._FreeInt32Array = self._dll.FreeLibraryAllocatedInt32Array
        self._FreeInt32Array.argtypes = [ctypes.c_void_p]
        self._FreeInt32Array.restype = None

        # UninitializeOCR()
        try:
            self._UninitializeOCR = self._dll.UninitializeOCR
            self._UninitializeOCR.argtypes = []
            self._UninitializeOCR.restype = None
        except AttributeError:
            self._UninitializeOCR = None

        logger.info("DLL loaded and functions resolved successfully")

    def get_version(self):
        """Get library version."""
        major = ctypes.c_uint32(0)
        minor = ctypes.c_uint32(0)
        self._GetLibraryVersion(ctypes.byref(major), ctypes.byref(minor))
        return major.value, minor.value

    def initialize(self, debug=False, light_mode=False):
        """
        Full initialization sequence:
        1. Load DLL
        2. Get version
        3. Set logger (if available)
        4. Enable debug (optional)
        5. Preload model files
        6. Set file content callbacks
        7. Init OCR pipeline
        """
        # Step 1: Load DLL
        self._load_dll()

        # Step 2: Version check
        major, minor = self.get_version()
        logger.info(f"Screen AI Library version: {major}.{minor}")

        # Step 3: Logger
        if self._SetLogger:
            log_cb = self._make_logger_callback()
            self._SetLogger(log_cb)
            logger.info("Logger callback registered")

        # Step 4: Debug mode
        if debug:
            self._EnableDebugMode()
            logger.info("Debug mode enabled (protos will be saved to temp)")

        # Step 5: Preload model files
        self._preload_model_files()

        # Step 6: Register file I/O callbacks
        size_fn, content_fn = self._make_file_callbacks()
        self._SetFileContentFunctions(size_fn, content_fn)
        logger.info("File content callbacks registered")

        # Step 6.5: Light mode (optional)
        if light_mode and self._SetOCRLightMode:
            self._SetOCRLightMode(True)
            logger.info("OCR Light Mode enabled")

        # Step 7: Initialize OCR pipeline
        logger.info("Initializing OCR pipeline (loading models)...")
        with _suppress_native_stderr():
            success = self._InitOCR()

        if not success:
            raise RuntimeError("InitOCRUsingCallback() returned false - model loading failed")

        self._initialized = True
        logger.info("OCR pipeline initialized successfully!")

        # Query max dimension
        if self._GetMaxImageDimension:
            max_dim = self._GetMaxImageDimension()
            logger.info(f"Max image dimension (no downscale): {max_dim}px")

    def _create_skbitmap(self, pixels_bgra, width, height):
        """
        Construct a fake SkBitmap struct from raw BGRA pixel data.

        Args:
            pixels_bgra: ctypes array or bytes-like of BGRA pixel data
            width: Image width in pixels
            height: Image height in pixels

        Returns:
            (SkBitmap instance, references_to_keep_alive)
        """
        row_bytes = width * 4  # BGRA = 4 bytes per pixel

        # Get pointer to pixel data
        if isinstance(pixels_bgra, ctypes.Array):
            pixel_ptr = ctypes.addressof(pixels_bgra)
        else:
            # Convert to ctypes array
            buf = (ctypes.c_uint8 * len(pixels_bgra)).from_buffer_copy(pixels_bgra)
            pixel_ptr = ctypes.addressof(buf)
            pixels_bgra = buf  # keep reference

        # Create fake SkPixelRef (in case DLL checks fPixelRef != nullptr)
        fake_pr = FakeSkPixelRef()
        fake_pr.vtable_ptr = 0  # NULL - hope DLL doesn't call virtuals
        fake_pr.fRefCnt = 1     # ref count = 1
        fake_pr.fWidth = width
        fake_pr.fHeight = height
        fake_pr.fPixels = pixel_ptr
        fake_pr.fRowBytes = row_bytes
        fake_pr.fTaggedGenID = 1
        self._fake_pixel_ref = fake_pr  # prevent GC

        # Construct SkBitmap
        bm = SkBitmap()
        bm.fPixelRef_ptr = ctypes.addressof(fake_pr)
        bm.fPixmap_fPixels = pixel_ptr
        bm.fPixmap_fRowBytes = row_bytes
        bm.fColorSpace_ptr = 0   # nullptr = default sRGB
        # kN32 = BGRA on Windows, RGBA on macOS/Linux
        bm.fColorType = kBGRA_8888_SkColorType if sys.platform == "win32" else kRGBA_8888_SkColorType
        bm.fAlphaType = kPremul_SkAlphaType      # 2
        bm.fWidth = width
        bm.fHeight = height
        bm.fMips_ptr = 0  # nullptr

        return bm, (pixels_bgra, fake_pr)

    def perform_ocr_raw(self, pixels_bgra, width, height):
        """
        Run OCR on raw BGRA pixel data.

        Args:
            pixels_bgra: Raw BGRA pixel buffer (bytes or ctypes array)
            width: Image width
            height: Image height

        Returns:
            Raw bytes of serialized VisualAnnotation protobuf, or None on failure.
        """
        if not self._initialized:
            raise RuntimeError("OCR not initialized. Call initialize() first.")

        # Create SkBitmap
        bitmap, refs = self._create_skbitmap(pixels_bgra, width, height)

        # Call PerformOCR (suppress DLL's verbose C++ logging)
        result_length = ctypes.c_uint32(0)
        with _suppress_native_stderr():
            result_ptr = self._PerformOCR(
                ctypes.byref(bitmap),
                ctypes.byref(result_length)
            )

        if result_ptr is None or result_ptr == 0:
            logger.error("PerformOCR returned nullptr")
            return None

        # Copy result data before freeing
        length = result_length.value
        result_data = ctypes.string_at(result_ptr, length)

        # Free the library-allocated buffer
        self._FreeCharArray(result_ptr)

        return result_data

    def perform_ocr(self, image):
        """
        Run OCR on a PIL Image or numpy array.

        Args:
            image: PIL.Image.Image or numpy.ndarray (H, W, 3 or H, W, 4)

        Returns:
            dict with parsed VisualAnnotation containing OCR results:
            {
                "lines": [
                    {
                        "utf8_string": "detected text line",
                        "language": "en",
                        "bounding_box": {"x": 0, "y": 0, "width": 100, "height": 20},
                        "words": [
                            {
                                "utf8_string": "detected",
                                "bounding_box": {...},
                                "has_space_after": True,
                                ...
                            },
                            ...
                        ]
                    },
                    ...
                ]
            }
        """
        # Convert to native pixel format (BGRA on Windows, RGBA on macOS/Linux)
        pixels_bgra, width, height = self._image_to_native_pixels(image)

        # Run OCR
        raw_proto = self.perform_ocr_raw(pixels_bgra, width, height)
        if raw_proto is None:
            return {"lines": []}

        # Parse protobuf
        return parse_visual_annotation(raw_proto)

    def perform_ocr_text(self, image):
        """
        Convenience: Run OCR and return just the text.

        Returns:
            str: All detected text joined by newlines.
        """
        result = self.perform_ocr(image)
        lines = []
        for line in result.get("lines", []):
            text = line.get("utf8_string", "")
            if not text:
                # Reconstruct from words
                words = []
                for word in line.get("words", []):
                    words.append(word.get("utf8_string", ""))
                    if word.get("has_space_after", False):
                        words.append(" ")
                text = "".join(words)
            lines.append(text)
        return "\n".join(lines)

    @staticmethod
    def _image_to_native_pixels(image):
        """
        Convert PIL Image or numpy array to native pixel buffer.
        Windows: BGRA (kN32 = kBGRA_8888)
        macOS/Linux: RGBA (kN32 = kRGBA_8888)

        Returns:
            (ctypes_array, width, height)
        """
        need_swap = (sys.platform == "win32")  # Only Windows needs RGBA→BGRA swap

        try:
            import numpy as np
            has_numpy = True
        except ImportError:
            has_numpy = False

        # Handle PIL Image
        try:
            from PIL import Image
            if isinstance(image, Image.Image):
                width, height = image.size

                if need_swap:
                    if has_numpy:
                        # Fast path: RGB→BGRA in one allocation (avoid RGBA intermediate)
                        src_mode = image.mode
                        if src_mode not in ("RGB", "RGBA"):
                            image = image.convert("RGB")
                            src_mode = "RGB"

                        arr = np.array(image)
                        bgra = np.empty((height, width, 4), dtype=np.uint8)

                        if src_mode == "RGB":
                            bgra[:, :, 0] = arr[:, :, 2]  # B
                            bgra[:, :, 1] = arr[:, :, 1]  # G
                            bgra[:, :, 2] = arr[:, :, 0]  # R
                            bgra[:, :, 3] = 255            # A
                        else:
                            # RGBA → BGRA
                            bgra[:, :, 0] = arr[:, :, 2]  # B
                            bgra[:, :, 1] = arr[:, :, 1]  # G
                            bgra[:, :, 2] = arr[:, :, 0]  # R
                            bgra[:, :, 3] = arr[:, :, 3]  # A

                        bgra = np.ascontiguousarray(bgra)
                        buf = (ctypes.c_uint8 * bgra.nbytes).from_buffer(bgra)
                        return buf, width, height
                    else:
                        if image.mode != "RGBA":
                            image = image.convert("RGBA")
                        rgba_data = image.tobytes()
                        buf_arr = bytearray(len(rgba_data))
                        for i in range(0, len(rgba_data), 4):
                            buf_arr[i] = rgba_data[i + 2]      # B
                            buf_arr[i + 1] = rgba_data[i + 1]  # G
                            buf_arr[i + 2] = rgba_data[i]      # R
                            buf_arr[i + 3] = rgba_data[i + 3]  # A
                        pixel_data = bytes(buf_arr)
                        buf = (ctypes.c_uint8 * len(pixel_data)).from_buffer_copy(pixel_data)
                        return buf, width, height
                else:
                    # macOS/Linux: RGBA is already native
                    if image.mode != "RGBA":
                        image = image.convert("RGBA")
                    pixel_data = image.tobytes()
                    buf = (ctypes.c_uint8 * len(pixel_data)).from_buffer_copy(pixel_data)
                    return buf, width, height
        except ImportError:
            pass

        # Handle numpy array
        if has_numpy and isinstance(image, np.ndarray):
            if image.ndim == 2:
                h, w = image.shape
                out = np.zeros((h, w, 4), dtype=np.uint8)
                if need_swap:
                    out[:, :, 0] = image  # B
                    out[:, :, 1] = image  # G
                    out[:, :, 2] = image  # R
                else:
                    out[:, :, 0] = image  # R
                    out[:, :, 1] = image  # G
                    out[:, :, 2] = image  # B
                out[:, :, 3] = 255        # A
            elif image.shape[2] == 3:
                h, w, _ = image.shape
                out = np.zeros((h, w, 4), dtype=np.uint8)
                if need_swap:
                    out[:, :, 0] = image[:, :, 2]  # B
                    out[:, :, 1] = image[:, :, 1]  # G
                    out[:, :, 2] = image[:, :, 0]  # R
                else:
                    out[:, :, :3] = image           # RGB as-is
                out[:, :, 3] = 255                  # A
            elif image.shape[2] == 4:
                h, w, _ = image.shape
                if need_swap:
                    out = image.copy()
                    out[:, :, 0] = image[:, :, 2]  # B
                    out[:, :, 2] = image[:, :, 0]  # R
                else:
                    out = image  # RGBA already native
            else:
                raise ValueError(f"Unsupported array shape: {image.shape}")

            width, height = w, h
            pixel_data = out.tobytes()
            buf = (ctypes.c_uint8 * len(pixel_data)).from_buffer_copy(pixel_data)
            return buf, width, height

        raise TypeError(f"Unsupported image type: {type(image)}")

    def shutdown(self):
        """Cleanup and unload."""
        if self._UninitializeOCR and self._initialized:
            try:
                self._UninitializeOCR()
            except Exception as e:
                logger.warning(f"UninitializeOCR error: {e}")

        self._initialized = False
        self._file_cache.clear()
        self._dll = None
        if self._dll_dir_handle is not None:
            try:
                self._dll_dir_handle.close()
            except Exception:
                pass
            self._dll_dir_handle = None
        logger.info("Screen AI OCR shut down")

    def __del__(self):
        try:
            self.shutdown()
        except:
            pass

    def __enter__(self):
        self.initialize()
        return self

    def __exit__(self, *args):
        self.shutdown()


# ===========================================================================
# Approach C: Direct TFLite API (bypass SkBitmap entirely)
# ===========================================================================
class TFLiteDirectOCR:
    """
    Alternative approach: Use the TFLite C API exported from chrome_screen_ai.dll
    to load and run the detection model directly.

    This bypasses the SkBitmap requirement entirely but loses the full GOCR pipeline
    (multi-script routing, language models, layout analysis).

    Useful as a fallback if SkBitmap construction fails.

    Pipeline (manual):
      1. Load text detection model (gocr_group_rpn_text_detection_model_2024_q4.tflite)
      2. Run detection -> get bounding boxes of text regions
      3. For each region, load appropriate recognition model
      4. Run recognition -> get character sequences

    This is significantly more complex but doesn't depend on Skia ABI.
    """

    def __init__(self, dll_path=None, model_dir=None):
        if dll_path is None or model_dir is None:
            auto_dll, auto_model = ScreenAIOCR._auto_detect_paths()
            dll_path = dll_path or auto_dll
            model_dir = model_dir or auto_model

        self.dll_path = dll_path
        self.model_dir = model_dir
        self._dll = None
        self._dll_dir_handle = None

    def _load_tflite_api(self):
        """Load TFLite C API functions from the native library."""
        dll_dir = os.path.dirname(self.dll_path)
        if sys.platform == "win32":
            self._dll_dir_handle = os.add_dll_directory(dll_dir)
        elif sys.platform == "darwin":
            os.environ.setdefault("DYLD_LIBRARY_PATH", dll_dir)
        else:
            ld_path = os.environ.get("LD_LIBRARY_PATH", "")
            os.environ["LD_LIBRARY_PATH"] = f"{dll_dir}:{ld_path}" if ld_path else dll_dir
        self._dll = ctypes.CDLL(self.dll_path)

        # TfLiteVersion() -> const char*
        self._TfLiteVersion = self._dll.TfLiteVersion
        self._TfLiteVersion.argtypes = []
        self._TfLiteVersion.restype = ctypes.c_char_p

        # TfLiteModelCreateFromFile(const char* model_path) -> TfLiteModel*
        self._TfLiteModelCreateFromFile = self._dll.TfLiteModelCreateFromFile
        self._TfLiteModelCreateFromFile.argtypes = [ctypes.c_char_p]
        self._TfLiteModelCreateFromFile.restype = ctypes.c_void_p

        # TfLiteModelCreate(const void* model_data, size_t model_size) -> TfLiteModel*
        self._TfLiteModelCreate = self._dll.TfLiteModelCreate
        self._TfLiteModelCreate.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        self._TfLiteModelCreate.restype = ctypes.c_void_p

        # TfLiteModelDelete(TfLiteModel*)
        self._TfLiteModelDelete = self._dll.TfLiteModelDelete
        self._TfLiteModelDelete.argtypes = [ctypes.c_void_p]
        self._TfLiteModelDelete.restype = None

        # TfLiteInterpreterOptionsCreate() -> TfLiteInterpreterOptions*
        self._TfLiteInterpreterOptionsCreate = self._dll.TfLiteInterpreterOptionsCreate
        self._TfLiteInterpreterOptionsCreate.argtypes = []
        self._TfLiteInterpreterOptionsCreate.restype = ctypes.c_void_p

        # TfLiteInterpreterOptionsDelete(TfLiteInterpreterOptions*)
        self._TfLiteInterpreterOptionsDelete = self._dll.TfLiteInterpreterOptionsDelete
        self._TfLiteInterpreterOptionsDelete.argtypes = [ctypes.c_void_p]
        self._TfLiteInterpreterOptionsDelete.restype = None

        # TfLiteInterpreterOptionsSetNumThreads(TfLiteInterpreterOptions*, int32_t)
        self._TfLiteInterpreterOptionsSetNumThreads = self._dll.TfLiteInterpreterOptionsSetNumThreads
        self._TfLiteInterpreterOptionsSetNumThreads.argtypes = [ctypes.c_void_p, ctypes.c_int32]
        self._TfLiteInterpreterOptionsSetNumThreads.restype = None

        # XNNPack delegate for acceleration
        # TfLiteXNNPackDelegateOptionsDefault() -> TfLiteXNNPackDelegateOptions
        # TfLiteXNNPackDelegateCreate(const TfLiteXNNPackDelegateOptions*) -> TfLiteDelegate*
        # TfLiteInterpreterOptionsAddDelegate(TfLiteInterpreterOptions*, TfLiteDelegate*)

        # TfLiteInterpreterCreate(TfLiteModel*, TfLiteInterpreterOptions*) -> TfLiteInterpreter*
        self._TfLiteInterpreterCreate = self._dll.TfLiteInterpreterCreate
        self._TfLiteInterpreterCreate.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._TfLiteInterpreterCreate.restype = ctypes.c_void_p

        # TfLiteInterpreterDelete(TfLiteInterpreter*)
        self._TfLiteInterpreterDelete = self._dll.TfLiteInterpreterDelete
        self._TfLiteInterpreterDelete.argtypes = [ctypes.c_void_p]
        self._TfLiteInterpreterDelete.restype = None

        # TfLiteInterpreterAllocateTensors(TfLiteInterpreter*) -> TfLiteStatus
        self._TfLiteInterpreterAllocateTensors = self._dll.TfLiteInterpreterAllocateTensors
        self._TfLiteInterpreterAllocateTensors.argtypes = [ctypes.c_void_p]
        self._TfLiteInterpreterAllocateTensors.restype = ctypes.c_int  # 0=OK, 1=Error

        # TfLiteInterpreterGetInputTensorCount(TfLiteInterpreter*) -> int32_t
        self._TfLiteInterpreterGetInputTensorCount = self._dll.TfLiteInterpreterGetInputTensorCount
        self._TfLiteInterpreterGetInputTensorCount.argtypes = [ctypes.c_void_p]
        self._TfLiteInterpreterGetInputTensorCount.restype = ctypes.c_int32

        # TfLiteInterpreterGetOutputTensorCount(TfLiteInterpreter*) -> int32_t
        self._TfLiteInterpreterGetOutputTensorCount = self._dll.TfLiteInterpreterGetOutputTensorCount
        self._TfLiteInterpreterGetOutputTensorCount.argtypes = [ctypes.c_void_p]
        self._TfLiteInterpreterGetOutputTensorCount.restype = ctypes.c_int32

        # TfLiteInterpreterGetInputTensor(TfLiteInterpreter*, int32_t) -> TfLiteTensor*
        self._TfLiteInterpreterGetInputTensor = self._dll.TfLiteInterpreterGetInputTensor
        self._TfLiteInterpreterGetInputTensor.argtypes = [ctypes.c_void_p, ctypes.c_int32]
        self._TfLiteInterpreterGetInputTensor.restype = ctypes.c_void_p

        # TfLiteInterpreterGetOutputTensor(TfLiteInterpreter*, int32_t) -> const TfLiteTensor*
        self._TfLiteInterpreterGetOutputTensor = self._dll.TfLiteInterpreterGetOutputTensor
        self._TfLiteInterpreterGetOutputTensor.argtypes = [ctypes.c_void_p, ctypes.c_int32]
        self._TfLiteInterpreterGetOutputTensor.restype = ctypes.c_void_p

        # TfLiteInterpreterResizeInputTensor(TfLiteInterpreter*, int32_t, const int*, int32_t) -> TfLiteStatus
        self._TfLiteInterpreterResizeInputTensor = self._dll.TfLiteInterpreterResizeInputTensor
        self._TfLiteInterpreterResizeInputTensor.argtypes = [
            ctypes.c_void_p, ctypes.c_int32, ctypes.POINTER(ctypes.c_int), ctypes.c_int32
        ]
        self._TfLiteInterpreterResizeInputTensor.restype = ctypes.c_int

        # TfLiteInterpreterInvoke(TfLiteInterpreter*) -> TfLiteStatus
        self._TfLiteInterpreterInvoke = self._dll.TfLiteInterpreterInvoke
        self._TfLiteInterpreterInvoke.argtypes = [ctypes.c_void_p]
        self._TfLiteInterpreterInvoke.restype = ctypes.c_int

        # TfLiteTensorCopyFromBuffer(TfLiteTensor*, const void*, size_t) -> TfLiteStatus
        self._TfLiteTensorCopyFromBuffer = self._dll.TfLiteTensorCopyFromBuffer
        self._TfLiteTensorCopyFromBuffer.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        self._TfLiteTensorCopyFromBuffer.restype = ctypes.c_int

        # TfLiteTensorCopyToBuffer(const TfLiteTensor*, void*, size_t) -> TfLiteStatus
        self._TfLiteTensorCopyToBuffer = self._dll.TfLiteTensorCopyToBuffer
        self._TfLiteTensorCopyToBuffer.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        self._TfLiteTensorCopyToBuffer.restype = ctypes.c_int

        # TfLiteTensorNumDims(const TfLiteTensor*) -> int32_t
        self._TfLiteTensorNumDims = self._dll.TfLiteTensorNumDims
        self._TfLiteTensorNumDims.argtypes = [ctypes.c_void_p]
        self._TfLiteTensorNumDims.restype = ctypes.c_int32

        # TfLiteTensorDim(const TfLiteTensor*, int32_t) -> int32_t
        self._TfLiteTensorDim = self._dll.TfLiteTensorDim
        self._TfLiteTensorDim.argtypes = [ctypes.c_void_p, ctypes.c_int32]
        self._TfLiteTensorDim.restype = ctypes.c_int32

        # TfLiteTensorByteSize(const TfLiteTensor*) -> size_t
        self._TfLiteTensorByteSize = self._dll.TfLiteTensorByteSize
        self._TfLiteTensorByteSize.argtypes = [ctypes.c_void_p]
        self._TfLiteTensorByteSize.restype = ctypes.c_size_t

        # TfLiteTensorType(const TfLiteTensor*) -> TfLiteType
        self._TfLiteTensorType = self._dll.TfLiteTensorType
        self._TfLiteTensorType.argtypes = [ctypes.c_void_p]
        self._TfLiteTensorType.restype = ctypes.c_int  # enum

        # TfLiteTensorName(const TfLiteTensor*) -> const char*
        self._TfLiteTensorName = self._dll.TfLiteTensorName
        self._TfLiteTensorName.argtypes = [ctypes.c_void_p]
        self._TfLiteTensorName.restype = ctypes.c_char_p

        # TfLiteTensorData(const TfLiteTensor*) -> void*
        self._TfLiteTensorData = self._dll.TfLiteTensorData
        self._TfLiteTensorData.argtypes = [ctypes.c_void_p]
        self._TfLiteTensorData.restype = ctypes.c_void_p

    def probe_model(self, model_filename):
        """
        Load a .tflite model and print its input/output tensor info.
        Useful for reverse-engineering the model's expected data format.

        Args:
            model_filename: Relative path to .tflite file within model_dir
        """
        self._load_tflite_api()

        version = self._TfLiteVersion()
        print(f"TFLite version: {version.decode()}")

        model_path = os.path.join(self.model_dir, model_filename.replace("/", os.sep))
        print(f"\nLoading model: {model_path}")

        model = self._TfLiteModelCreateFromFile(model_path.encode("utf-8"))
        if not model:
            print("ERROR: Failed to load model")
            return

        options = self._TfLiteInterpreterOptionsCreate()
        self._TfLiteInterpreterOptionsSetNumThreads(options, 4)

        interpreter = self._TfLiteInterpreterCreate(model, options)
        if not interpreter:
            print("ERROR: Failed to create interpreter")
            self._TfLiteModelDelete(model)
            self._TfLiteInterpreterOptionsDelete(options)
            return

        status = self._TfLiteInterpreterAllocateTensors(interpreter)
        if status != 0:
            print(f"WARNING: AllocateTensors returned status {status}")

        # TfLiteType enum mapping
        type_names = {
            0: "kTfLiteNoType", 1: "kTfLiteFloat32", 2: "kTfLiteInt32",
            3: "kTfLiteUInt8", 4: "kTfLiteInt64", 5: "kTfLiteString",
            6: "kTfLiteBool", 7: "kTfLiteInt16", 8: "kTfLiteComplex64",
            9: "kTfLiteInt8", 10: "kTfLiteFloat16", 11: "kTfLiteFloat64",
        }

        # Input tensors
        n_inputs = self._TfLiteInterpreterGetInputTensorCount(interpreter)
        print(f"\n=== INPUT TENSORS ({n_inputs}) ===")
        for i in range(n_inputs):
            tensor = self._TfLiteInterpreterGetInputTensor(interpreter, i)
            name = self._TfLiteTensorName(tensor)
            ttype = self._TfLiteTensorType(tensor)
            ndims = self._TfLiteTensorNumDims(tensor)
            dims = [self._TfLiteTensorDim(tensor, d) for d in range(ndims)]
            byte_size = self._TfLiteTensorByteSize(tensor)

            print(f"  [{i}] name={name.decode() if name else '?'}")
            print(f"       type={type_names.get(ttype, ttype)}, dims={dims}, bytes={byte_size}")

        # Output tensors
        n_outputs = self._TfLiteInterpreterGetOutputTensorCount(interpreter)
        print(f"\n=== OUTPUT TENSORS ({n_outputs}) ===")
        for i in range(n_outputs):
            tensor = self._TfLiteInterpreterGetOutputTensor(interpreter, i)
            name = self._TfLiteTensorName(tensor)
            ttype = self._TfLiteTensorType(tensor)
            ndims = self._TfLiteTensorNumDims(tensor)
            dims = [self._TfLiteTensorDim(tensor, d) for d in range(ndims)]
            byte_size = self._TfLiteTensorByteSize(tensor)

            print(f"  [{i}] name={name.decode() if name else '?'}")
            print(f"       type={type_names.get(ttype, ttype)}, dims={dims}, bytes={byte_size}")

        # Cleanup
        self._TfLiteInterpreterDelete(interpreter)
        self._TfLiteInterpreterOptionsDelete(options)
        self._TfLiteModelDelete(model)
        print("\nModel probe complete.")


# ===========================================================================
# CLI Test
# ===========================================================================
if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python screen_ai_ocr.py test <image_path>     # Test OCR on image")
        print("  python screen_ai_ocr.py probe <model.tflite>  # Probe TFLite model")
        print("  python screen_ai_ocr.py version               # Show DLL version")
        print("  python screen_ai_ocr.py exports                # List DLL exports")
        sys.exit(1)

    command = sys.argv[1]

    if command == "version":
        ocr = ScreenAIOCR()
        ocr._load_dll()
        major, minor = ocr.get_version()
        print(f"Screen AI Library v{major}.{minor}")

    elif command == "exports":
        # List DLL exports using pefile
        try:
            import pefile
        except ImportError:
            print("Install pefile: pip install pefile")
            sys.exit(1)

        ocr = ScreenAIOCR()
        pe = pefile.PE(ocr.dll_path)
        print(f"Exports from {ocr.dll_path}:")
        for exp in pe.DIRECTORY_ENTRY_EXPORT.symbols:
            name = exp.name.decode() if exp.name else f"ordinal_{exp.ordinal}"
            print(f"  {name}")

    elif command == "probe":
        if len(sys.argv) < 3:
            print("Usage: python screen_ai_ocr.py probe <model_relative_path>")
            print("Example: python screen_ai_ocr.py probe gocr/gocr_models/detection/gocr_group_rpn_text_detection_model_2024_q4.tflite")
            sys.exit(1)

        tflite = TFLiteDirectOCR()
        tflite.probe_model(sys.argv[2])

    elif command == "test":
        if len(sys.argv) < 3:
            print("Usage: python screen_ai_ocr.py test <image_path>")
            sys.exit(1)

        image_path = sys.argv[2]

        try:
            from PIL import Image
        except ImportError:
            print("Install Pillow: pip install Pillow")
            sys.exit(1)

        print(f"Loading image: {image_path}")
        img = Image.open(image_path)
        print(f"Image size: {img.size}, mode: {img.mode}")

        print("\n--- Initializing Screen AI OCR ---")
        ocr = ScreenAIOCR()

        try:
            ocr.initialize(debug=True)

            print("\n--- Running OCR ---")
            result = ocr.perform_ocr(img)

            print(f"\n--- Results: {len(result['lines'])} lines ---")
            for i, line in enumerate(result["lines"]):
                lang = line.get("language", "?")
                text = line.get("utf8_string", "")
                bbox = line.get("bounding_box", {})
                print(f"  Line {i+1} [{lang}] ({bbox.get('x',0)},{bbox.get('y',0)} "
                      f"{bbox.get('width',0)}x{bbox.get('height',0)}): {text}")

            print("\n--- Full Text ---")
            print(ocr.perform_ocr_text(img))

        except Exception as e:
            logger.error(f"OCR failed: {e}", exc_info=True)
        finally:
            ocr.shutdown()

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)
