"""
screen_ai_downloader.py - Auto-download Chrome ScreenAI component from Google
==============================================================================
Downloads the OCR library directly from Google's Component Updater CDN.
No Chrome installation required. Works on Windows, macOS, Linux.

Component ID: mfhmdacoffpmifoibamicehhklffanao
Protocol: Chromium CRX3 via clients2.google.com
"""

import hashlib
import io
import logging
import os
import platform
import shutil
import struct
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

COMPONENT_ID = "mfhmdacoffpmifoibamicehhklffanao"
BASE_URL = "https://clients2.google.com/service/update2/crx"


class ScreenAIIntegrityError(RuntimeError):
    """Raised when ScreenAI binaries fail integrity verification."""


# ── Authenticode verification (Windows-only) ────────────────────────
# Verifies the extracted chrome_screen_ai.dll has a valid Authenticode
# signature AND that the signer is Google LLC. Defense-in-depth on top
# of the SHA256 check from Google's update XML — catches tampering of
# the on-disk DLL after extraction (e.g. local malware modifying it).
_GOOGLE_SIGNER_NEEDLES = ("Google LLC", "Google Inc")


def _verify_authenticode_google_signed(file_path: str) -> None:
    """Verify `file_path` is Authenticode-signed by Google LLC.

    Windows-only. On other platforms this is a no-op (caller should rely
    on the SHA256 check from Google's update XML for those builds).

    Raises ScreenAIIntegrityError on any failure: invalid signature,
    untrusted chain, or signer not Google.
    """
    if sys.platform != "win32":
        logger.info("Authenticode verify skipped (non-Windows platform)")
        return

    import subprocess

    # Single-quoted PowerShell strings escape ' as ''.
    escaped = file_path.replace("'", "''")
    ps_script = (
        "$ErrorActionPreference='Stop';"
        f"$sig = Get-AuthenticodeSignature -FilePath '{escaped}';"
        "if ($sig.Status -ne 'Valid') {"
        "  Write-Error (\"Authenticode status: \" + $sig.Status);"
        "  exit 1;"
        "}"
        "Write-Output $sig.SignerCertificate.Subject"
    )

    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-Command", ps_script],
            capture_output=True, text=True, timeout=20,
        )
    except FileNotFoundError as e:
        raise ScreenAIIntegrityError(
            f"powershell.exe not available — cannot run Authenticode "
            f"verification on {file_path}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise ScreenAIIntegrityError(
            f"Authenticode verification timed out for {file_path}"
        ) from e

    if result.returncode != 0:
        raise ScreenAIIntegrityError(
            f"Authenticode signature invalid for {file_path}\n"
            f"  PowerShell stderr: {(result.stderr or '').strip()}\n"
            f"  PowerShell stdout: {(result.stdout or '').strip()}"
        )

    subject = (result.stdout or "").strip()
    if not any(needle in subject for needle in _GOOGLE_SIGNER_NEEDLES):
        raise ScreenAIIntegrityError(
            f"DLL not signed by Google. Subject:\n  {subject}"
        )

    logger.info(f"Authenticode OK ({subject})")

# Library file name per platform
LIBRARY_NAMES = {
    "win32":  "chrome_screen_ai.dll",
    "darwin": "libchromescreenai.dylib",
    "linux":  "libchromescreenai.so",
}

# SkBitmap kN32_SkColorType per platform
# Windows: BGRA (6), macOS/Linux: RGBA (4)
PLATFORM_COLOR_TYPE = {
    "win32":  6,  # kBGRA_8888_SkColorType
    "darwin": 4,  # kRGBA_8888_SkColorType
    "linux":  4,  # kRGBA_8888_SkColorType
}


def _get_platform_params():
    """Get OS/arch params for the download URL."""
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "windows":
        os_name = "win"
        arch = "x64" if machine in ("amd64", "x86_64") else "x86"
        os_arch = "x86_64" if arch == "x64" else "x86"
    elif system == "darwin":
        os_name = "mac"
        arch = "arm64" if machine == "arm64" else "x64"
        os_arch = arch
    else:  # Linux
        os_name = "linux"
        arch = "x64"
        os_arch = "x86_64"

    return os_name, arch, os_arch


def _build_url(response_type="redirect"):
    """Build the Component Updater URL."""
    os_name, arch, os_arch = _get_platform_params()
    return (
        f"{BASE_URL}?response={response_type}"
        f"&os={os_name}&arch={arch}&os_arch={os_arch}"
        f"&nacl_arch=x86-64"
        f"&prod=chromiumcrx&prodchannel=unknown&prodversion=130.0.0.0"
        f"&acceptformat=crx3"
        f"&x=id%3D{COMPONENT_ID}%26uc"
    )


def check_update():
    """
    Check available version from Google.

    Returns:
        dict with keys: version, url, sha256, size
        or None if check fails
    """
    url = _build_url("updatecheck")
    logger.info(f"Checking for updates: {url}")

    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=15) as resp:
            xml_data = resp.read().decode("utf-8")
    except (URLError, OSError) as e:
        logger.error(f"Update check failed: {e}")
        return None

    try:
        root = ElementTree.fromstring(xml_data)
        # Parse XML: <gupdate><app><updatecheck ... /></app></gupdate>
        for app in root.iter():
            if app.tag == "updatecheck" or app.tag.endswith("}updatecheck"):
                status = app.get("status", "")
                if status != "ok":
                    logger.warning(f"Update check status: {status}")
                    return None

                codebase = app.get("codebase", "")
                sha256 = app.get("hash_sha256", "")
                size = int(app.get("size", "0"))
                # Extract version from codebase URL
                # URL format: ..._140.21/mfhmdacoffp..._140.21_win64_...crx3
                version = app.get("version", "")
                if not version and codebase:
                    import re
                    m = re.search(r'_(\d+\.\d+)_', codebase)
                    if m:
                        version = m.group(1)
                if not version:
                    version = "unknown"

                return {
                    "version": version,
                    "url": codebase,
                    "sha256": sha256,
                    "size": size,
                }
    except ElementTree.ParseError as e:
        logger.error(f"Failed to parse update XML: {e}")

    return None


def download_crx(dest_path, progress_callback=None):
    """
    Download the ScreenAI CRX3 file.

    Args:
        dest_path: Where to save the CRX3 file
        progress_callback: fn(downloaded_bytes, total_bytes) for progress

    Returns:
        True if successful
    """
    url = _build_url("redirect")
    logger.info(f"Downloading ScreenAI from Google CDN...")

    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 256 * 1024  # 256KB chunks

            with open(dest_path, "wb") as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total)

        logger.info(f"Downloaded {downloaded:,} bytes to {dest_path}")
        return True

    except (URLError, OSError) as e:
        logger.error(f"Download failed: {e}")
        if os.path.exists(dest_path):
            os.remove(dest_path)
        return False


def extract_crx3(crx_path, dest_dir):
    """
    Extract CRX3 file (ZIP with prepended header).

    CRX3 format:
      - 4 bytes: magic "Cr24"
      - 4 bytes: version (uint32 LE) = 3
      - 4 bytes: header_length (uint32 LE)
      - header_length bytes: signed header (protobuf)
      - remaining: ZIP archive

    Args:
        crx_path: Path to CRX3 file
        dest_dir: Directory to extract to

    Returns:
        True if successful
    """
    logger.info(f"Extracting CRX3: {crx_path}")

    try:
        with open(crx_path, "rb") as f:
            magic = f.read(4)
            if magic != b"Cr24":
                raise ValueError(f"Not a CRX3 file (magic: {magic})")

            version = struct.unpack("<I", f.read(4))[0]
            if version != 3:
                raise ValueError(f"Unsupported CRX version: {version}")

            header_len = struct.unpack("<I", f.read(4))[0]
            f.seek(12 + header_len)  # Skip to ZIP data
            zip_data = f.read()

        os.makedirs(dest_dir, exist_ok=True)

        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            zf.extractall(dest_dir)

        logger.info(f"Extracted to: {dest_dir}")
        return True

    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        return False


def get_installed_version(model_dir):
    """
    Check what version is installed locally.

    Args:
        model_dir: Base directory (e.g. models/screen_ai/)

    Returns:
        (version_string, full_path) or (None, None)
    """
    if not os.path.isdir(model_dir):
        return None, None

    # Find version subdirectories
    versions = []
    for entry in os.listdir(model_dir):
        entry_path = os.path.join(model_dir, entry)
        if os.path.isdir(entry_path):
            # Check if it has the library file
            lib_name = LIBRARY_NAMES.get(sys.platform, "chrome_screen_ai.dll")
            if os.path.exists(os.path.join(entry_path, lib_name)):
                versions.append((entry, entry_path))

    if not versions:
        return None, None

    # Return latest version (sort by version string)
    versions.sort(key=lambda x: x[0], reverse=True)
    return versions[0]


def _get_chrome_screen_ai_base_path():
    """Get the expected Chrome ScreenAI path for the current platform."""
    if sys.platform == "win32":
        return os.path.expandvars(
            r"%LOCALAPPDATA%\Google\Chrome\User Data\screen_ai"
        )
    elif sys.platform == "darwin":
        return os.path.expanduser(
            "~/Library/Application Support/Google/Chrome/screen_ai"
        )
    else:
        return os.path.expanduser("~/.config/google-chrome/screen_ai")


def find_chrome_screen_ai():
    """
    Try to find ScreenAI from Chrome's local installation.

    Returns:
        (version, path) or (None, None)
    """
    base = _get_chrome_screen_ai_base_path()

    if not os.path.isdir(base):
        return None, None

    return get_installed_version(base)


class ScreenAIStatus:
    """Result of check_screen_ai() — tells caller what's available."""
    FOUND_LOCAL = "found_local"       # Already in model_dir
    FOUND_CHROME = "found_chrome"     # Found in Chrome profile, can copy
    NEED_DOWNLOAD = "need_download"   # Must download from Google CDN
    UNAVAILABLE = "unavailable"       # No internet, can't download

    def __init__(self, status, lib_path=None, model_path=None,
                 color_type=6, version=None, download_size=0,
                 chrome_path=None, message=""):
        self.status = status
        self.lib_path = lib_path
        self.model_path = model_path
        self.color_type = color_type
        self.version = version
        self.download_size = download_size  # bytes
        self.chrome_path = chrome_path
        self.message = message

    @property
    def ready(self):
        return self.status in (self.FOUND_LOCAL, self.FOUND_CHROME)

    @property
    def size_mb(self):
        return self.download_size / (1024 * 1024)


def check_screen_ai(model_dir):
    """
    Check ScreenAI availability WITHOUT downloading anything.
    Returns ScreenAIStatus so the caller (UI) can ask user before proceeding.

    Priority:
      1. Already in model_dir → FOUND_LOCAL (ready to use)
      2. Found in Chrome profile → FOUND_CHROME (ready to copy, no download)
      3. Can reach Google CDN → NEED_DOWNLOAD (needs user consent)
      4. No internet → UNAVAILABLE

    Args:
        model_dir: Base directory for models (e.g. "models/screen_ai")

    Returns:
        ScreenAIStatus
    """
    lib_name = LIBRARY_NAMES.get(sys.platform, "chrome_screen_ai.dll")
    color_type = PLATFORM_COLOR_TYPE.get(sys.platform, 6)

    # --- Step 1: Check local model_dir ---
    ver, ver_path = get_installed_version(model_dir)
    if ver and ver_path:
        lib_path = os.path.join(ver_path, lib_name)
        return ScreenAIStatus(
            ScreenAIStatus.FOUND_LOCAL,
            lib_path=lib_path, model_path=ver_path,
            color_type=color_type, version=ver,
            message=f"ScreenAI v{ver} found locally"
        )

    # --- Step 2: Check Chrome installation ---
    chrome_ver, chrome_path = find_chrome_screen_ai()
    if chrome_ver and chrome_path:
        return ScreenAIStatus(
            ScreenAIStatus.FOUND_CHROME,
            color_type=color_type, version=chrome_ver,
            chrome_path=chrome_path,
            message=f"Tìm thấy thư viện OCR v{chrome_ver} trong Chrome. Copy về dùng nhé!"
        )

    # --- Step 3: Check Google CDN ---
    # Build Chrome profile path for display
    chrome_profile_path = _get_chrome_screen_ai_base_path()

    update_info = check_update()
    if update_info:
        version = update_info.get("version", "unknown")
        size = update_info.get("size", 0)
        size_mb = size / (1024 * 1024)
        return ScreenAIStatus(
            ScreenAIStatus.NEED_DOWNLOAD,
            color_type=color_type, version=version,
            download_size=size,
            message=(
                f"Không tìm thấy thư viện OCR đi kèm, "
                f"cũng không tìm thấy trong {chrome_profile_path}.\n"
                f"Cho phép tải ScreenAI v{version} ({size_mb:.0f} MB) từ Google về không?"
            )
        )

    return ScreenAIStatus(
        ScreenAIStatus.UNAVAILABLE,
        message=(
            f"Không tìm thấy thư viện OCR đi kèm, "
            f"cũng không tìm thấy trong {chrome_profile_path}.\n"
            f"Không thể kết nối Google để tải. Kiểm tra kết nối mạng hoặc cài Google Chrome."
        )
    )


def install_screen_ai(model_dir, status, progress_callback=None, log_callback=None):
    """
    Install ScreenAI based on a previous check_screen_ai() result.
    Call this ONLY after user has consented.

    Args:
        model_dir: Base directory for models
        status: ScreenAIStatus from check_screen_ai()
        progress_callback: fn(downloaded, total) for download progress
        log_callback: fn(message, level) for status messages

    Returns:
        (library_path, model_path, color_type) or raises RuntimeError
    """
    def log(msg, level="info"):
        logger.info(msg)
        if log_callback:
            try:
                log_callback(msg, level)
            except Exception:
                pass

    lib_name = LIBRARY_NAMES.get(sys.platform, "chrome_screen_ai.dll")
    color_type = status.color_type

    # Already local
    if status.status == ScreenAIStatus.FOUND_LOCAL:
        _verify_authenticode_google_signed(status.lib_path)
        return status.lib_path, status.model_path, color_type

    # Copy from Chrome
    if status.status == ScreenAIStatus.FOUND_CHROME:
        log(f"Copying ScreenAI v{status.version} from Chrome...", "info")
        dest = os.path.join(model_dir, status.version)
        os.makedirs(model_dir, exist_ok=True)
        try:
            shutil.copytree(status.chrome_path, dest, dirs_exist_ok=True)
            lib_path = os.path.join(dest, lib_name)
            if os.path.exists(lib_path):
                _verify_authenticode_google_signed(lib_path)
                log(f"ScreenAI ready: {dest}", "success")
                return lib_path, dest, color_type
        except ScreenAIIntegrityError:
            raise
        except Exception as e:
            log(f"Copy failed: {e}. Will try downloading...", "error")

    # Download from Google CDN
    if status.status not in (ScreenAIStatus.NEED_DOWNLOAD, ScreenAIStatus.FOUND_CHROME):
        raise RuntimeError(status.message)

    log("Downloading ScreenAI from Google...", "info")

    update_info = check_update()
    if not update_info:
        raise RuntimeError(
            "Failed to check ScreenAI version from Google servers. "
            "Check internet connection."
        )

    version = update_info.get("version", "unknown")
    size_mb = update_info.get("size", 0) / (1024 * 1024)
    log(f"Downloading ScreenAI v{version} ({size_mb:.0f} MB)...", "info")

    # Download CRX3
    with tempfile.NamedTemporaryFile(suffix=".crx3", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        def _progress(downloaded, total):
            if progress_callback:
                progress_callback(downloaded, total)
            if total > 0:
                pct = downloaded * 100 / total
                if int(pct) % 10 == 0:
                    log(f"Downloading: {pct:.0f}%", "debug")

        if not download_crx(tmp_path, _progress):
            raise RuntimeError("Failed to download ScreenAI from Google CDN")

        # D — verify SHA256 announced by Google's update XML.
        # Required: refuse to extract a CRX whose hash Google did not vouch for.
        expected_sha = update_info.get("sha256", "")
        if not expected_sha:
            raise ScreenAIIntegrityError(
                "Google update XML did not include hash_sha256; refusing to "
                "extract an unverified CRX."
            )
        sha = hashlib.sha256()
        with open(tmp_path, "rb") as f:
            for chunk in iter(lambda: f.read(256 * 1024), b""):
                sha.update(chunk)
        actual_sha = sha.hexdigest()
        if actual_sha != expected_sha:
            raise ScreenAIIntegrityError(
                f"CRX SHA256 mismatch.\n"
                f"  Expected (from Google XML): {expected_sha}\n"
                f"  Got (downloaded file):       {actual_sha}"
            )
        log(f"CRX SHA256 verified OK (Google announced: {expected_sha[:16]}...)", "info")

        # Extract
        # Try to determine version from manifest after extraction
        extract_dir = os.path.join(model_dir, "_temp_extract")
        if not extract_crx3(tmp_path, extract_dir):
            raise RuntimeError("Failed to extract CRX3 file")

        # Read version from manifest.json
        manifest_path = os.path.join(extract_dir, "manifest.json")
        if os.path.exists(manifest_path):
            import json
            with open(manifest_path, "r") as f:
                manifest = json.load(f)
            version = manifest.get("version", version)

        # Move to final location
        final_dir = os.path.join(model_dir, version)
        if os.path.exists(final_dir):
            shutil.rmtree(final_dir)
        shutil.move(extract_dir, final_dir)

        lib_path = os.path.join(final_dir, lib_name)
        if not os.path.exists(lib_path):
            raise RuntimeError(
                f"Library {lib_name} not found after extraction. "
                f"Platform {sys.platform} may not be supported."
            )

        # B — Authenticode verify the on-disk DLL is genuinely signed by
        # Google. Catches post-extraction tampering that the SHA256 check
        # above (D) cannot see.
        _verify_authenticode_google_signed(lib_path)

        log(f"ScreenAI v{version} installed: {final_dir}", "success")
        return lib_path, final_dir, color_type

    finally:
        # Cleanup temp files
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        extract_dir = os.path.join(model_dir, "_temp_extract")
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir, ignore_errors=True)


# ── CLI usage ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    from scanindex.infra.paths import get_base_dir

    model_dir = os.path.join(get_base_dir(), "models", "screen_ai")

    if "--check" in sys.argv:
        info = check_update()
        if info:
            print(f"Version:  {info['version']}")
            print(f"Size:     {info['size']:,} bytes")
            print(f"SHA256:   {info['sha256']}")
            print(f"URL:      {info['url']}")
        else:
            print("Failed to check update")
        sys.exit(0)

    if "--find-chrome" in sys.argv:
        ver, path = find_chrome_screen_ai()
        if ver:
            print(f"Found in Chrome: v{ver} at {path}")
        else:
            print("Not found in Chrome")
        sys.exit(0)

    # Default: check → ask user → install
    def progress(downloaded, total):
        if total > 0:
            pct = downloaded * 100 / total
            bar = "█" * int(pct / 2) + "░" * (50 - int(pct / 2))
            print(f"\r  [{bar}] {pct:.0f}% ({downloaded/1024/1024:.1f}MB)", end="", flush=True)

    # Step 1: Check (no download)
    status = check_screen_ai(model_dir)
    print(f"Status:  {status.status}")
    print(f"Message: {status.message}")

    if status.status == ScreenAIStatus.FOUND_LOCAL:
        print(f"\nReady! (already installed)")
        print(f"  Library:   {status.lib_path}")
        print(f"  Models:    {status.model_path}")
        sys.exit(0)

    if status.status == ScreenAIStatus.FOUND_CHROME:
        print(f"\nFound in Chrome v{status.version}. Copy to local? (no download needed)")

    if status.status == ScreenAIStatus.NEED_DOWNLOAD:
        print(f"\nNeed to download v{status.version} ({status.size_mb:.0f} MB) from Google.")

    if status.status == ScreenAIStatus.UNAVAILABLE:
        print("\nScreenAI not available. Install Chrome or check internet.")
        sys.exit(1)

    # Step 2: Ask user consent
    answer = input("Proceed? [y/N] ").strip().lower()
    if answer != "y":
        print("Cancelled.")
        sys.exit(0)

    # Step 3: Install (only after consent)
    try:
        lib_path, mod_path, color_type = install_screen_ai(
            model_dir, status, progress_callback=progress
        )
        print(f"\n\nReady!")
        print(f"  Library:    {lib_path}")
        print(f"  Models:     {mod_path}")
        print(f"  ColorType:  {color_type} ({'BGRA' if color_type == 6 else 'RGBA'})")
    except RuntimeError as e:
        print(f"\nError: {e}")
        sys.exit(1)
