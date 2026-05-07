"""
Windows Certificate Store access and CSP/CNG-based signing.

Works with any USB token whose driver registers a CSP/KSP in Windows.
PIN prompt is handled by the token driver.
"""

import ctypes
import ctypes.wintypes as wintypes
import hashlib
import unicodedata
from ctypes import windll, POINTER, byref
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple

from asn1crypto import x509 as asn1_x509

# ── Win32 constants ─────────────────────────────────────────────────────────
CERT_SYSTEM_STORE_CURRENT_USER = 0x00010000
CERT_NCRYPT_KEY_SPEC = 0xFFFFFFFF        # returned by CryptAcquireCertificatePrivateKey when key is CNG
CRYPT_ACQUIRE_PREFER_NCRYPT_KEY_FLAG = 0x20000
CRYPT_ACQUIRE_CACHE_FLAG = 0x1
BCRYPT_PAD_PKCS1 = 0x2
CALG_SHA1   = 0x8004
CALG_SHA256 = 0x800C
CALG_SHA384 = 0x800D
CALG_SHA512 = 0x800E

# ── DLL handles ─────────────────────────────────────────────────────────────
_crypt32  = ctypes.WinDLL("crypt32.dll")
_advapi32 = ctypes.WinDLL("advapi32.dll")
_ncrypt   = ctypes.WinDLL("ncrypt.dll")


# ── Function signatures (mandatory on x64 — default int return truncates pointers) ──
_crypt32.CertOpenSystemStoreW.restype  = ctypes.c_void_p
_crypt32.CertOpenSystemStoreW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]

_crypt32.CertCloseStore.restype  = wintypes.BOOL
_crypt32.CertCloseStore.argtypes = [ctypes.c_void_p, wintypes.DWORD]

_crypt32.CertEnumCertificatesInStore.restype  = ctypes.c_void_p
_crypt32.CertEnumCertificatesInStore.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

_crypt32.CertDuplicateCertificateContext.restype  = ctypes.c_void_p
_crypt32.CertDuplicateCertificateContext.argtypes = [ctypes.c_void_p]

_crypt32.CertFreeCertificateContext.restype  = wintypes.BOOL
_crypt32.CertFreeCertificateContext.argtypes = [ctypes.c_void_p]

_crypt32.CryptAcquireCertificatePrivateKey.restype  = wintypes.BOOL
_crypt32.CryptAcquireCertificatePrivateKey.argtypes = [
    ctypes.c_void_p,                # PCCERT_CONTEXT
    wintypes.DWORD,                 # dwFlags
    ctypes.c_void_p,                # pvParameters
    ctypes.POINTER(ctypes.c_void_p),  # phCryptProvOrNCryptKey
    ctypes.POINTER(wintypes.DWORD), # pdwKeySpec
    ctypes.POINTER(wintypes.BOOL),  # pfCallerFreeProvOrNCryptKey
]

_advapi32.CryptCreateHash.restype  = wintypes.BOOL
_advapi32.CryptCreateHash.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
]
_advapi32.CryptHashData.restype  = wintypes.BOOL
_advapi32.CryptHashData.argtypes = [
    ctypes.c_void_p, ctypes.c_char_p, wintypes.DWORD, wintypes.DWORD,
]
_advapi32.CryptSignHashW.restype  = wintypes.BOOL
_advapi32.CryptSignHashW.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
]
_advapi32.CryptDestroyHash.restype  = wintypes.BOOL
_advapi32.CryptDestroyHash.argtypes = [ctypes.c_void_p]
_advapi32.CryptReleaseContext.restype  = wintypes.BOOL
_advapi32.CryptReleaseContext.argtypes = [ctypes.c_void_p, wintypes.DWORD]

_ncrypt.NCryptSignHash.restype  = ctypes.c_long   # SECURITY_STATUS (HRESULT-like)
_ncrypt.NCryptSignHash.argtypes = [
    ctypes.c_void_p,                # NCRYPT_KEY_HANDLE
    ctypes.c_void_p,                # pPaddingInfo
    ctypes.c_char_p, wintypes.DWORD,  # pbHashValue, cbHashValue
    ctypes.c_void_p, wintypes.DWORD,  # pbSignature, cbSignature
    ctypes.POINTER(wintypes.DWORD),   # pcbResult
    wintypes.DWORD,                 # dwFlags
]
_ncrypt.NCryptFreeObject.restype  = ctypes.c_long
_ncrypt.NCryptFreeObject.argtypes = [ctypes.c_void_p]


# ── CERT_CONTEXT structure ───────────────────────────────────────────────────
class _CERT_CONTEXT(ctypes.Structure):
    _fields_ = [
        ("dwCertEncodingType", wintypes.DWORD),
        ("pbCertEncoded",      POINTER(ctypes.c_ubyte)),
        ("cbCertEncoded",      wintypes.DWORD),
        ("pCertInfo",          ctypes.c_void_p),
        ("hCertStore",         ctypes.c_void_p),
    ]


class _BCRYPT_PKCS1_PADDING_INFO(ctypes.Structure):
    _fields_ = [("pszAlgId", ctypes.c_wchar_p)]


# ── Helper maps ──────────────────────────────────────────────────────────────
_CALG = {"sha1": CALG_SHA1, "sha256": CALG_SHA256,
         "sha384": CALG_SHA384, "sha512": CALG_SHA512}
_BCRYPT_ALG = {"sha1": "SHA1", "sha256": "SHA256",
               "sha384": "SHA384", "sha512": "SHA512"}

_TRUSTED_SIGNING_ISSUER_MARKERS = (
    # Names embedded in VGCA SignService/SignTool trusted CA resources.
    "ban co yeu chinh phu",
    "co quan chung thuc so chuyen dung chinh phu",
    "co quan chung thuc so chinh phu",
    "ca phuc vu cac co quan nha nuoc",
    "ca phuc vu cac co quan dang",
    "ca phuc vu cho dich vu phan mem",
    "cuc chung thuc so va bao mat thong tin",
    "co quan chung thuc so bo cong an",
    "co quan chung thuc so bo tai chinh",
    "co quan chung thuc so dang cong san",
    "fpt certification authority",
    "vnpt certification authority",
    "smartsign",
    "national centre of digital signature authentication",
)
_SIGNING_KEY_USAGES = {"digital_signature", "non_repudiation", "content_commitment"}
_DOCUMENT_SIGNING_EKUS = {
    "adobe_authentic_documents_trust",
    "email_protection",
    "microsoft_document_signing",
}


def _get_attr(name, oid_dotted: str) -> Optional[str]:
    """Extract a single attribute value from an asn1crypto Name by OID.

    Name.chosen is an RDNSequence — a list of RDNs, each of which is a SET
    of AttributeTypeAndValue. Need a nested loop.
    """
    for rdn in name.chosen:
        for attr in rdn:
            if attr["type"].dotted == oid_dotted:
                val = attr["value"].native
                return str(val) if val is not None else None
    return None


def _fold_text(value: str) -> str:
    """Case-insensitive, accent-insensitive text for issuer matching."""
    normalised = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in normalised if not unicodedata.combining(ch)).casefold()


def _as_utc(value):
    if not hasattr(value, "tzinfo"):
        return value
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _extension_value(cert: asn1_x509.Certificate, ext_name: str):
    try:
        for ext in cert["tbs_certificate"]["extensions"]:
            if ext["extn_id"].native == ext_name:
                return ext["extn_value"].native
    except Exception:
        return None
    return None


def _is_vgca_signing_certificate(cert: asn1_x509.Certificate, now=None) -> bool:
    """Approximate VGCA's default certificate picker filter before signing."""
    now = now or datetime.now(timezone.utc)

    try:
        tbs = cert["tbs_certificate"]
        not_before = _as_utc(tbs["validity"]["not_before"].native)
        not_after = _as_utc(tbs["validity"]["not_after"].native)
        if now < not_before or now > not_after:
            return False

        subject_text = _fold_text(cert.subject.human_friendly)
        issuer_text = _fold_text(cert.issuer.human_friendly)
        if subject_text == issuer_text:
            return False

        if cert.public_key.algorithm != "rsa":
            return False

        basic_constraints = _extension_value(cert, "basic_constraints") or {}
        if isinstance(basic_constraints, dict) and basic_constraints.get("ca"):
            return False

        key_usage = _extension_value(cert, "key_usage")
        if key_usage is not None and not (_SIGNING_KEY_USAGES & set(key_usage)):
            return False

        eku = _extension_value(cert, "extended_key_usage")
        if eku and not (_DOCUMENT_SIGNING_EKUS & set(eku)):
            return False

        return any(marker in issuer_text for marker in _TRUSTED_SIGNING_ISSUER_MARKERS)
    except Exception:
        return False


# ── Public API ───────────────────────────────────────────────────────────────

def list_certificates(store_name: str = "MY", signing_only: bool = True) -> List[Dict]:
    """
    List certificates with accessible private keys in the Windows cert store.

    By default, prefer valid end-user signing certificates issued by the CA set
    embedded in VGCA's tools. If no such certificate is found, return the
    private-key certificates as a fallback.
    Returns a list of dicts ready for display and for use with sign_data().
    """
    h_store = _crypt32.CertOpenSystemStoreW(0, store_name)
    if not h_store:
        raise ctypes.WinError()

    certs: List[Dict] = []
    signing_certs: List[Dict] = []
    p_ctx = None
    now = datetime.now(timezone.utc)

    try:
        while True:
            p_ctx = _crypt32.CertEnumCertificatesInStore(h_store, p_ctx)
            if not p_ctx:
                break

            cert_ctx = ctypes.cast(p_ctx, POINTER(_CERT_CONTEXT)).contents
            der = bytes(cert_ctx.pbCertEncoded[: cert_ctx.cbCertEncoded])

            try:
                cert = asn1_x509.Certificate.load(der)
                has_key, is_cng = _has_private_key(p_ctx)
                if not has_key:
                    continue

                subj = cert.subject
                cn  = _get_attr(subj, "2.5.4.3")   # commonName
                org = _get_attr(subj, "2.5.4.10")  # organizationName
                ou  = _get_attr(subj, "2.5.4.11")  # organizationalUnitName

                tbs = cert["tbs_certificate"]
                not_after = tbs["validity"]["not_after"].native
                key_usage = _extension_value(cert, "key_usage") or []
                extended_key_usage = _extension_value(cert, "extended_key_usage") or []
                basic_constraints = _extension_value(cert, "basic_constraints") or {}
                is_ca = bool(
                    isinstance(basic_constraints, dict)
                    and basic_constraints.get("ca")
                )
                signing_eligible = _is_vgca_signing_certificate(cert, now)

                # Duplicate context so it stays valid after store closes
                p_dup = _crypt32.CertDuplicateCertificateContext(p_ctx)
                if not p_dup:
                    continue

                display = "{} (còn hạn đến {})".format(
                    cn or org or "Unknown",
                    not_after.strftime("%Y-%m-%d") if hasattr(not_after, "strftime") else str(not_after)[:10],
                )

                entry = {
                    "cn":          cn or "",
                    "org":         org or "",
                    "ou":          ou or "",
                    "subject":     subj.human_friendly,
                    "issuer":      cert.issuer.human_friendly,
                    "serial":      tbs["serial_number"].native,
                    "not_after":   not_after,
                    "key_usage":   sorted(key_usage) if key_usage else [],
                    "extended_key_usage": list(extended_key_usage) if extended_key_usage else [],
                    "is_ca":       is_ca,
                    "signing_eligible": signing_eligible,
                    "is_cng":      is_cng,
                    "der":         der,
                    "cert_ctx":    p_dup,   # PCCERT_CONTEXT, must be freed
                    "display":     display,
                }
                certs.append(entry)
                if signing_eligible:
                    signing_certs.append(entry)
            except Exception:
                pass
    finally:
        # CertEnumCertificatesInStore takes ownership of p_ctx for the last
        # returned value; passing NULL frees that last context automatically.
        _crypt32.CertCloseStore(h_store, 0)

    if signing_only and signing_certs:
        signing_ids = {id(c) for c in signing_certs}
        free_cert_contexts([c for c in certs if id(c) not in signing_ids])
        return signing_certs

    return certs


def free_cert_contexts(cert_list: List[Dict]) -> None:
    """Release duplicated CERT_CONTEXT handles when no longer needed."""
    for c in cert_list:
        ctx = c.get("cert_ctx")
        if ctx:
            _crypt32.CertFreeCertificateContext(ctx)
            c["cert_ctx"] = None


def sign_data(cert_info: Dict, data: bytes, hash_alg: str = "sha256") -> bytes:
    """
    Sign *data* using the private key bound to *cert_info* (from list_certificates).

    Handles both:
      - CNG keys  → NCryptSignHash (most modern USB tokens)
      - Legacy CSP → CryptSignHash  (older tokens / software certs)

    Returns the raw RSA/ECDSA signature in big-endian byte order, as
    expected by pyHanko's async_sign_raw().
    """
    p_ctx = cert_info["cert_ctx"]

    h_key = wintypes.HANDLE()
    dw_key_spec = wintypes.DWORD()
    pf_free = wintypes.BOOL()

    ok = _crypt32.CryptAcquireCertificatePrivateKey(
        p_ctx,
        CRYPT_ACQUIRE_PREFER_NCRYPT_KEY_FLAG,
        None,
        byref(h_key),
        byref(dw_key_spec),
        byref(pf_free),
    )
    if not ok:
        raise ctypes.WinError()

    is_cng = dw_key_spec.value == CERT_NCRYPT_KEY_SPEC
    try:
        if is_cng:
            return _sign_cng(h_key.value, data, hash_alg)
        else:
            return _sign_legacy_csp(h_key.value, dw_key_spec.value, data, hash_alg)
    finally:
        if pf_free.value:
            if is_cng:
                _ncrypt.NCryptFreeObject(h_key)
            else:
                _advapi32.CryptReleaseContext(h_key, 0)


# ── Internal helpers ─────────────────────────────────────────────────────────

def _has_private_key(p_ctx) -> Tuple[bool, bool]:
    """Returns (has_key, is_cng). Does NOT keep the key handle open."""
    h_key = wintypes.HANDLE()
    dw_key_spec = wintypes.DWORD()
    pf_free = wintypes.BOOL()

    ok = _crypt32.CryptAcquireCertificatePrivateKey(
        p_ctx,
        CRYPT_ACQUIRE_PREFER_NCRYPT_KEY_FLAG,
        None,
        byref(h_key),
        byref(dw_key_spec),
        byref(pf_free),
    )
    if not ok:
        return False, False

    is_cng = dw_key_spec.value == CERT_NCRYPT_KEY_SPEC
    if pf_free.value:
        if is_cng:
            _ncrypt.NCryptFreeObject(h_key)
        else:
            _advapi32.CryptReleaseContext(h_key, 0)
    return True, is_cng


def _sign_cng(key_handle: int, data: bytes, hash_alg: str) -> bytes:
    """NCryptSignHash with PKCS#1 v1.5 padding. Returns big-endian bytes."""
    digest = hashlib.new(hash_alg, data).digest()
    alg_name = _BCRYPT_ALG[hash_alg.lower()]
    padding = _BCRYPT_PKCS1_PADDING_INFO(alg_name)

    sig_len = wintypes.DWORD(0)
    status = _ncrypt.NCryptSignHash(
        key_handle, byref(padding),
        digest, len(digest),
        None, 0, byref(sig_len),
        BCRYPT_PAD_PKCS1,
    )
    if status:
        raise OSError(f"NCryptSignHash (query size) failed: 0x{status:08X}")

    buf = (ctypes.c_ubyte * sig_len.value)()
    status = _ncrypt.NCryptSignHash(
        key_handle, byref(padding),
        digest, len(digest),
        buf, sig_len, byref(sig_len),
        BCRYPT_PAD_PKCS1,
    )
    if status:
        raise OSError(f"NCryptSignHash failed: 0x{status:08X}")

    return bytes(buf[: sig_len.value])   # already big-endian


def _sign_legacy_csp(h_prov: int, key_spec: int, data: bytes, hash_alg: str) -> bytes:
    """CryptSignHash. Returns big-endian bytes (legacy API is little-endian, reversed here)."""
    alg_id = _CALG[hash_alg.lower()]
    h_hash = wintypes.HANDLE()

    if not _advapi32.CryptCreateHash(h_prov, alg_id, 0, 0, byref(h_hash)):
        raise ctypes.WinError()
    try:
        if not _advapi32.CryptHashData(h_hash, data, len(data), 0):
            raise ctypes.WinError()

        sig_len = wintypes.DWORD(0)
        _advapi32.CryptSignHashW(h_hash, key_spec, None, 0, None, byref(sig_len))

        buf = (ctypes.c_ubyte * sig_len.value)()
        if not _advapi32.CryptSignHashW(h_hash, key_spec, None, 0, buf, byref(sig_len)):
            raise ctypes.WinError()

        # Legacy CryptoAPI returns RSA sig in little-endian → reverse to big-endian
        return bytes(reversed(buf[: sig_len.value]))
    finally:
        _advapi32.CryptDestroyHash(h_hash)
