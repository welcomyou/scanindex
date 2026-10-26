"""Explicit consent for local-time signing in both Qt signing workflows."""
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QObject, QThread
from PySide6.QtWidgets import QApplication, QMessageBox

from scanindex.core.pdf.signer import TSASigningError
from scanindex.ui.digitization import signing_step as archive
from scanindex.ui.screens import batch_signing_screen as bulk
from scanindex.ui.widgets import signing_config_panel as panel_module


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(params=["archive", "bulk"])
def workflow(request):
    if request.param == "archive":
        return archive, archive._SignWorker, archive.ArchiveStep3Sign._on_tsa_failure
    return bulk, bulk._BatchSignWorker, bulk.BatchSigningScreen._on_tsa_failure


def make_worker(workflow, tmp_path, count=2):
    module, worker_type, _ = workflow
    items = []
    for index in range(count):
        source = tmp_path / f"{index}.pdf"
        source.write_bytes(b"original PDF input (signing is mocked)")
        items.append(module._SignItem(source_path=str(source), display_name=source.name))
    kwargs = dict(items=items, output_dir=str(tmp_path / "out"), cert_info={},
                  sig_box=(0, 0, 220, 60), custom_page=0, stamp_template="Test",
                  tsa_url="https://tsa.invalid/", enable_pdfa=False)
    if module is bulk:
        kwargs["auto_ocr"] = False
    return worker_type(**kwargs)


def run_worker(app, worker):
    results, errors = [], []
    worker.finished_ok.connect(results.append)
    worker.failed.connect(errors.append)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not results and not errors and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.002)
        assert not errors, errors
        assert results, "Worker stayed blocked waiting for a TSA decision"
        return results[0]
    finally:
        worker.cancel()
        assert worker.wait(1000), "Worker must stop after cancellation"
        app.processEvents()


class DecisionHarness(QObject):
    """Run the actual screen handler on the GUI thread without loading user config."""
    def __init__(self, app, workflow, worker, actions):
        super().__init__()
        self.app, self.workflow, self._worker = app, workflow, worker
        self.actions = iter(actions)
        self.prompts, self.logs = [], []
        self._panel = SimpleNamespace(_ask_local_time_fallback=self.ask)
        self.log_message = SimpleNamespace(emit=lambda *args: self.logs.append(args))

    def ask(self, detail, *, during_signing):
        assert QThread.currentThread() == self.app.thread()
        assert during_signing
        self.prompts.append(detail)
        return next(self.actions)

    def handle(self, detail):
        self.workflow[2](self, detail)


def test_tsa_failure_requires_consent_before_local_retry(app, workflow, tmp_path, monkeypatch):
    module = workflow[0]
    worker = make_worker(workflow, tmp_path)
    calls = []

    def sign(source, output, cert, **kwargs):
        calls.append((Path(source).name, kwargs["tsa_url"]))
        if kwargs["tsa_url"]:
            assert not Path(output).exists()
            raise TSASigningError("HTTP 503 from TSA")
        assert harness.prompts, "Local retry must not happen without a GUI confirmation"
        Path(output).write_bytes(b"signed using local computer time")

    monkeypatch.setattr(module, "sign_single_pdf", sign)
    harness = DecisionHarness(app, workflow, worker, ["local"])
    worker.tsa_failed.connect(harness.handle)
    results = run_worker(app, worker)
    assert [url for _, url in calls] == ["https://tsa.invalid/", "", ""]
    assert len(harness.prompts) == 1
    assert len(results) == 2 and all(r["ok"] for r in results)
    assert harness.logs
    for item in worker._items:
        assert Path(item.source_path).read_bytes().startswith(b"original")


def test_declining_local_time_stops_without_local_signature(app, workflow, tmp_path, monkeypatch):
    module = workflow[0]
    worker = make_worker(workflow, tmp_path)
    calls = []

    def sign(*args, **kwargs):
        calls.append(kwargs["tsa_url"])
        raise TSASigningError("TSA connection failed")

    monkeypatch.setattr(module, "sign_single_pdf", sign)
    harness = DecisionHarness(app, workflow, worker, ["cancel"])
    worker.tsa_failed.connect(harness.handle)
    assert run_worker(app, worker) == []
    assert calls == ["https://tsa.invalid/"]
    assert not list((tmp_path / "out").glob("*.pdf"))


def test_retry_tsa_does_not_switch_to_local_time(app, workflow, tmp_path, monkeypatch):
    module = workflow[0]
    worker = make_worker(workflow, tmp_path)
    calls = []

    def sign(source, output, cert, **kwargs):
        calls.append(kwargs["tsa_url"])
        if len(calls) == 1:
            raise TSASigningError("Temporary TSA failure")
        Path(output).write_bytes(b"signed with TSA")

    monkeypatch.setattr(module, "sign_single_pdf", sign)
    harness = DecisionHarness(app, workflow, worker, ["retry"])
    worker.tsa_failed.connect(harness.handle)
    results = run_worker(app, worker)
    assert len(results) == 2 and all(r["ok"] for r in results)
    assert calls == ["https://tsa.invalid/"] * 3
    assert len(harness.prompts) == 1


def test_later_tsa_failure_does_not_resign_completed_files(app, workflow, tmp_path, monkeypatch):
    module = workflow[0]
    worker = make_worker(workflow, tmp_path, count=3)
    calls = []

    def sign(source, output, cert, **kwargs):
        name = Path(source).name
        calls.append((name, kwargs["tsa_url"]))
        if name == "1.pdf" and kwargs["tsa_url"]:
            raise TSASigningError("TSA went offline after file 0")
        Path(output).write_bytes(b"TSA" if kwargs["tsa_url"] else b"LOCAL")

    monkeypatch.setattr(module, "sign_single_pdf", sign)
    harness = DecisionHarness(app, workflow, worker, ["local"])
    worker.tsa_failed.connect(harness.handle)
    results = run_worker(app, worker)
    assert len(results) == 3 and all(r["ok"] for r in results)
    assert [name for name, _ in calls] == ["0.pdf", "1.pdf", "1.pdf", "2.pdf"]
    assert (tmp_path / "out/0.pdf").read_bytes() == b"TSA"
    assert (tmp_path / "out/1.pdf").read_bytes() == b"LOCAL"
    assert (tmp_path / "out/2.pdf").read_bytes() == b"LOCAL"


def test_key_or_pdf_errors_never_prompt_for_tsa_fallback(app, workflow, tmp_path, monkeypatch):
    module = workflow[0]
    worker = make_worker(workflow, tmp_path)
    calls = []

    def sign(*args, **kwargs):
        calls.append(kwargs["tsa_url"])
        raise ValueError("PIN/token/PDF failure")

    monkeypatch.setattr(module, "sign_single_pdf", sign)
    harness = DecisionHarness(app, workflow, worker, [])
    worker.tsa_failed.connect(harness.handle)
    results = run_worker(app, worker)
    assert len(results) == 2 and all(not r["ok"] for r in results)
    assert calls == ["https://tsa.invalid/"] * 2
    assert not harness.prompts


def test_cancel_unblocks_a_worker_waiting_for_tsa_decision(app, workflow, tmp_path, monkeypatch):
    module = workflow[0]
    worker = make_worker(workflow, tmp_path)
    monkeypatch.setattr(module, "sign_single_pdf", lambda *a, **k: (_ for _ in ()).throw(TSASigningError("Unavailable")))
    worker.tsa_failed.connect(lambda detail: worker.cancel())
    assert run_worker(app, worker) == []


@pytest.mark.parametrize("action,expected_mode", [("local", "local_fallback"), ("cancel", None)])
def test_connection_failure_also_requires_confirmation(action, expected_mode):
    prompts = []

    def ask(detail):
        prompts.append(detail)
        return action

    panel = SimpleNamespace(_check_tsa_connection=lambda url: (False, "Offline"),
                            _ask_local_time_fallback=ask, log_message=SimpleNamespace(emit=lambda *a: None))
    result = panel_module.SigningConfigPanel.resolve_batch_time_decision(panel, "https://tsa.invalid/")
    assert prompts == ["Offline"]
    assert (result.mode if result else None) == expected_mode
    if result:
        assert result.tsa_url == ""


@pytest.mark.parametrize("url", ["http://tsa.invalid:bad/", "http://[::1", "http:///", "https://tsa.invalid:99999/"])
def test_malformed_tsa_address_returns_failure_instead_of_crashing(url):
    ok, detail = panel_module.SigningConfigPanel._check_tsa_connection(None, url)
    assert not ok and detail


@pytest.mark.parametrize("during_signing", [False, True])
def test_confirmation_dialog_explains_local_time_and_defaults_to_cancel(app, monkeypatch, during_signing):
    captured = []

    class Box(QMessageBox):
        def exec(self):
            captured.append(self)
            return 0

        def clickedButton(self):
            return next(button for button in self.buttons() if button.text() == "Đồng ý")

    monkeypatch.setattr(panel_module, "QMessageBox", Box)
    action = panel_module.SigningConfigPanel._ask_local_time_fallback(None, "TSA detail", during_signing=during_signing)
    assert action == "local"
    box, = captured
    assert "không phải là dấu thời gian tin cậy" in box.text()
    assert box.defaultButton().text() == "Hủy"
    assert box.detailedText() == "TSA detail"
    if during_signing:
        assert "các tệp còn lại" in box.informativeText()
        assert "Các tệp đã ký thành công" in box.informativeText()
