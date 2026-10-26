"""Native CNG EC/RSA signatures and certificate validity, using ephemeral keys.

No certificates are installed and no real token/PIN is used. Files stay in
pytest's temporary directory; CNG keys are released after every test.
"""
import asyncio
import ctypes
import http.server
from datetime import datetime, timedelta, timezone
import sys
import threading

import pytest

if sys.platform != "win32":
    pytest.skip("Windows CNG signing tests", allow_module_level=True)

from asn1crypto import x509 as asn1_x509
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import NameOID
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko_certvalidator import ValidationContext

from scanindex.core.pdf import signer, win_cert_store as store


def certificate(key, *, not_before=None, not_after=None, issuer=None):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Ephemeral signing test")])
    return (x509.CertificateBuilder().subject_name(name).issuer_name(issuer or name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(not_before or now - timedelta(days=1))
            .not_valid_after(not_after or now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(True, True, False, False, False, False, False, None, None), critical=True)
            .sign(key, hashes.SHA256()))


def info_for(cert):
    return {"der": cert.public_bytes(serialization.Encoding.DER), "cert_ctx": 1, "cn": "Test"}


@pytest.fixture(params=["rsa", "p256", "p384", "p521"])
def native_key(request, monkeypatch):
    algorithm = request.param
    curves = {"p256": ec.SECP256R1, "p384": ec.SECP384R1, "p521": ec.SECP521R1}
    key = (rsa.generate_private_key(public_exponent=65537, key_size=2048)
           if algorithm == "rsa" else ec.generate_private_key(curves[algorithm]()))
    encoded = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    provider, handle = ctypes.c_void_p(), ctypes.c_void_p()
    ncrypt = store._ncrypt
    ncrypt.NCryptOpenStorageProvider.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_wchar_p, ctypes.c_ulong]
    ncrypt.NCryptOpenStorageProvider.restype = ctypes.c_long
    ncrypt.NCryptImportKey.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p,
                                     ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p, ctypes.c_ulong, ctypes.c_ulong]
    ncrypt.NCryptImportKey.restype = ctypes.c_long
    status = ncrypt.NCryptOpenStorageProvider(ctypes.byref(provider), "Microsoft Software Key Storage Provider", 0)
    assert status == 0, hex(status & 0xFFFFFFFF)
    try:
        status = ncrypt.NCryptImportKey(provider, None, "PKCS8_PRIVATEKEY", None,
                                       ctypes.byref(handle), encoded, len(encoded), 0)
        assert status == 0, hex(status & 0xFFFFFFFF)

        def acquire(context, flags, parameters, output_key, key_spec, caller_free):
            output_key._obj.value = handle.value
            key_spec._obj.value = store.CERT_NCRYPT_KEY_SPEC
            caller_free._obj.value = False  # this fixture owns the ephemeral key
            return True

        monkeypatch.setattr(store._crypt32, "CryptAcquireCertificatePrivateKey", acquire)
        yield key, certificate(key), handle.value
    finally:
        if handle.value:
            ncrypt.NCryptFreeObject(handle)
        ncrypt.NCryptFreeObject(provider)


def test_native_sign_data_verifies_independently(native_key):
    key, cert, _ = native_key
    message = b"CMS attributes must be hashed exactly once"
    signature = store.sign_data(info_for(cert), message)
    if isinstance(key, rsa.RSAPrivateKey):
        cert.public_key().verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
        assert signature == key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    else:
        cert.public_key().verify(signature, message, ec.ECDSA(hashes.SHA256()))


def test_pdf_signed_with_native_key_retains_existing_profile(native_key, tmp_path):
    import fitz
    _, cert, _ = native_key
    source, output = tmp_path / "source.pdf", tmp_path / "signed.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(source)
    doc.close()
    signer.sign_single_pdf(str(source), str(output), info_for(cert))
    assert output.read_bytes().startswith(source.read_bytes())
    with output.open("rb") as stream:
        reader = PdfFileReader(stream)
        signature, = reader.embedded_signatures
        assert signature.field_name == "Signature1"
        assert signature.sig_object["/SubFilter"] == "/adbe.pkcs7.detached"
        assert "/DocMDP" not in reader.root.get("/Perms", {})
        context = ValidationContext(trust_roots=[asn1_x509.Certificate.load(info_for(cert)["der"])], allow_fetching=False)
        status = validate_pdf_signature(signature, signer_validation_context=context)
        assert status.intact and status.valid and status.trusted and status.docmdp_ok


def test_ec_dry_run_reserves_enough_der_space(native_key):
    _, cert, _ = native_key
    instance = signer.WinCSPSigner(info_for(cert))
    placeholder = asyncio.run(instance.async_sign_raw(b"test", "sha256", dry_run=True))
    signature = asyncio.run(instance.async_sign_raw(b"test", "sha256"))
    assert len(placeholder) >= len(signature)


def test_multiple_approval_signatures_preserve_prior_signatures(native_key, tmp_path):
    import fitz
    _, cert, _ = native_key
    source, first, second = [tmp_path / name for name in ("source.pdf", "first.pdf", "second.pdf")]
    doc = fitz.open()
    doc.new_page()
    doc.save(source)
    doc.close()
    signer.sign_single_pdf(str(source), str(first), info_for(cert))
    signer.sign_single_pdf(str(first), str(second), info_for(cert))
    assert second.read_bytes().startswith(first.read_bytes())
    with second.open("rb") as stream:
        reader = PdfFileReader(stream)
        assert [s.field_name for s in reader.embedded_signatures] == ["Signature1", "Signature2"]
        assert "/DocMDP" not in reader.root.get("/Perms", {})
        for signature in reader.embedded_signatures:
            context = ValidationContext(trust_roots=[asn1_x509.Certificate.load(info_for(cert)["der"])], allow_fetching=False)
            status = validate_pdf_signature(signature, signer_validation_context=context)
            assert status.intact and status.valid and status.trusted and status.docmdp_ok


def test_signature_name_avoids_text_and_parent_field_names(native_key, tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, TextStringObject
    writer = PdfWriter()
    writer.add_blank_page(width=400, height=600)
    text = writer._add_object(DictionaryObject({NameObject("/FT"): NameObject("/Tx"),
        NameObject("/T"): TextStringObject("Signature1")}))
    child = writer._add_object(DictionaryObject({NameObject("/FT"): NameObject("/Tx"),
        NameObject("/T"): TextStringObject("Child")}))
    parent = writer._add_object(DictionaryObject({NameObject("/T"): TextStringObject("Signature2"),
        NameObject("/Kids"): ArrayObject([child])}))
    child.get_object()[NameObject("/Parent")] = parent
    writer._root_object[NameObject("/AcroForm")] = writer._add_object(DictionaryObject({
        NameObject("/Fields"): ArrayObject([text, parent])}))
    source, output = tmp_path / "form.pdf", tmp_path / "signed-form.pdf"
    writer.write(source)
    _, cert, _ = native_key
    signer.sign_single_pdf(str(source), str(output), info_for(cert))
    with output.open("rb") as stream:
        reader = PdfFileReader(stream)
        assert reader.embedded_signatures[0].field_name == "Signature3"
        assert len(reader.root["/AcroForm"]["/Fields"]) == 3


def test_real_tsa_response_failure_is_typed_and_does_not_write_output(native_key, tmp_path):
    import fitz
    _, cert, _ = native_key

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html>No timestamp token</html>")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    source, output = tmp_path / "tsa-source.pdf", tmp_path / "keep-existing.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(source)
    doc.close()
    output.write_bytes(b"existing output must survive a failed attempt")
    try:
        with pytest.raises(signer.TSASigningError):
            signer.sign_single_pdf(str(source), str(output), info_for(cert),
                                   tsa_url=f"http://127.0.0.1:{server.server_port}/tsa")
        assert output.read_bytes() == b"existing output must survive a failed attempt"
        signer.sign_single_pdf(str(source), str(output), info_for(cert), tsa_url="")
        with output.open("rb") as stream:
            reader = PdfFileReader(stream)
            assert [s.field_name for s in reader.embedded_signatures] == ["Signature1"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_ec_passes_existing_picker_rules():
    key = ec.generate_private_key(ec.SECP256R1())
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "FPT Certification Authority Test")])
    cert = certificate(key, issuer=issuer)
    assert store._is_vgca_signing_certificate(asn1_x509.Certificate.load(info_for(cert)["der"]))


@pytest.mark.parametrize("offset,expected", [(-1, "chưa có hiệu lực"), (0, None), (30, None), (60, None), (61, "hết hạn")])
def test_validity_utc_boundaries(offset, expected):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    cert = certificate(key, not_before=start, not_after=start + timedelta(seconds=60))
    # Represent the same instant using Vietnam's UTC+7 zone.
    now = (start + timedelta(seconds=offset)).astimezone(timezone(timedelta(hours=7)))
    if expected:
        with pytest.raises(ValueError, match=expected):
            store.validate_certificate_validity(info_for(cert), now=now)
    else:
        store.validate_certificate_validity(info_for(cert), now=now)


@pytest.mark.parametrize("state", ["expired", "future"])
def test_invalid_certificate_rejected_before_key_or_pdf_access(state, monkeypatch, tmp_path):
    now = datetime.now(timezone.utc)
    start, end = ((now - timedelta(days=2), now - timedelta(days=1)) if state == "expired"
                  else (now + timedelta(days=1), now + timedelta(days=2)))
    cert = certificate(ec.generate_private_key(ec.SECP256R1()), not_before=start, not_after=end)
    info = info_for(cert)
    monkeypatch.setattr(store._crypt32, "CryptAcquireCertificatePrivateKey",
                        lambda *args: pytest.fail("An invalid certificate must never request a key/PIN"))
    expected = "hết hạn" if state == "expired" else "chưa có hiệu lực"
    with pytest.raises(ValueError, match=expected):
        store.sign_data(info, b"test")
    output = tmp_path / "must-not-exist.pdf"
    with pytest.raises(ValueError, match=expected):
        signer.sign_single_pdf("not-opened.pdf", str(output), info)
    assert not output.exists()


def test_legacy_provider_never_receives_ec_sign_request(monkeypatch):
    cert = certificate(ec.generate_private_key(ec.SECP256R1()))

    def acquire(context, flags, parameters, key, spec, free):
        key._obj.value, spec._obj.value, free._obj.value = 1, 2, False
        return True

    monkeypatch.setattr(store._crypt32, "CryptAcquireCertificatePrivateKey", acquire)
    monkeypatch.setattr(store, "_sign_legacy_csp", lambda *a: pytest.fail("EC must not use the RSA CSP path"))
    with pytest.raises(ValueError, match="CNG/KSP"):
        store.sign_data(info_for(cert), b"test")


@pytest.mark.parametrize("raw", [b"", b"x"])
def test_malformed_ecdsa_signature_is_rejected(raw, monkeypatch):
    def sign_hash(key, padding_info, digest, digest_size, output, output_size, result_size, flags):
        assert padding_info is None and flags == 0
        result_size._obj.value = len(raw)
        if output is not None and raw:
            ctypes.memmove(output, raw, len(raw))
        return 0

    monkeypatch.setattr(store._ncrypt, "NCryptSignHash", sign_hash)
    with pytest.raises(ValueError, match="P1363"):
        store._sign_cng(1, b"test", "sha256", "ec")
