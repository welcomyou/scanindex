# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the ScanIndex portable build.

Runtime data that the application mutates or loads through get_base_dir()
is copied by build_portable.bat next to the executable. This spec only bundles
Python packages and read-only in-bundle assets needed at import time.
"""

import os
import sys
from PyInstaller.utils.hooks import collect_all


block_cipher = None


# Pick up the version from scanindex/infra/version.py so the COLLECT dir
# matches build_portable.bat's DIST_DIR (`dist/ScanIndex-<version>`).
sys.path.insert(0, os.getcwd())
try:
    from scanindex.infra.version import get_version_short
    _APP_VERSION = get_version_short()
except Exception:
    _APP_VERSION = "dev"
_APP_NAME = "ScanIndex"
_COLLECT_NAME = f"{_APP_NAME}-{_APP_VERSION}"


datas = []
binaries = []
hiddenimports = []
include_correction = os.environ.get("INCLUDE_CORRECTION") == "1"


def add_data(src, dest=None):
    if os.path.exists(src):
        datas.append((src, dest or src))


# Read-only package data used through __file__/_MEIPASS.
add_data("assets", "assets")
add_data("scanindex/core/repository/schema.sql", "scanindex/core/repository")
add_data("settings.ini.example", ".")
add_data("ignored_words.txt", ".")
add_data("dictionaries", "dictionaries")
# VERSION file is written by build_portable.bat (from `git describe`)
# right before PyInstaller runs, so the bundled app reports the same
# version as the git tag the build was made from.
add_data("VERSION", ".")


# Runtime modules that are imported lazily or dynamically by the PySide UI.
# All business logic now lives under scanindex.* — root compatibility
# wrappers were removed, so only the entry point stays bare.
hiddenimports += [
    "ocr_app",

    # ScanIndex packages
    "scanindex",
    "scanindex.app",
    "scanindex.core",
    "scanindex.core.correction",
    "scanindex.core.correction.engine",
    "scanindex.core.digitization",
    "scanindex.core.digitization.doctype",
    "scanindex.core.digitization.fuzzy",
    "scanindex.core.digitization.metadata_export",
    "scanindex.core.digitization.metadata_extractor",
    "scanindex.core.digitization.page_splitter",
    "scanindex.core.digitization.runner",
    "scanindex.core.digitization.session",
    "scanindex.core.kie",
    "scanindex.core.kie.adjudication",
    "scanindex.core.kie.common",
    "scanindex.core.kie.engine",
    "scanindex.core.kie.exporters",
    "scanindex.core.kie.inference_pipeline",
    "scanindex.core.kie.json_utils",
    "scanindex.core.kie.labeling_workspace",
    "scanindex.core.kie.ontology",
    "scanindex.core.kie.postprocess",
    "scanindex.core.kie.semantic_fields",
    "scanindex.core.kie.text_normalize",
    "scanindex.core.preprocessing",
    "scanindex.core.preprocessing.preprocessing",
    "scanindex.core.ocr",
    "scanindex.core.ocr.accuracy_baseline",
    "scanindex.core.ocr.accuracy_metrics",
    "scanindex.core.ocr.direct_engine",
    "scanindex.core.ocr.screen_ai",
    "scanindex.core.ocr.screen_ai_downloader",
    "scanindex.core.ocr.text_normalizer",
    "scanindex.core.pdf",
    "scanindex.core.pdf.pdfa_converter",
    "scanindex.core.pdf.signer",
    "scanindex.core.pdf.text_extractor",
    "scanindex.core.pdf.utils",
    "scanindex.core.pdf.win_cert_store",
    "scanindex.core.repository",
    "scanindex.core.repository.admin",
    "scanindex.core.repository.chunker",
    "scanindex.core.repository.constants",
    "scanindex.core.repository.filter_builder",
    "scanindex.core.repository.importer",
    "scanindex.core.repository.indexer",
    "scanindex.core.repository.repair",
    "scanindex.core.repository.search_engine",
    "scanindex.core.repository.store",
    "scanindex.core.repository.tokenizer",
    "scanindex.core.tables",
    "scanindex.core.tables.docling_tableformer_engine",
    "scanindex.core.tables.docling_tableformer_v1_onnx_engine",
    "scanindex.core.tables.docling_tableformer_v2_onnx_engine",
    "scanindex.core.tables.docling_tableformer_v2_torch_engine",
    "scanindex.core.tables.docx_exporter",
    "scanindex.core.tables.eval_metrics",
    "scanindex.core.tables.export_worker",
    "scanindex.core.tables.gmft_onnx_table_engine",
    "scanindex.core.tables.layout_analyzer",
    "scanindex.core.tables.postprocess_v2",
    "scanindex.core.tables.rapidtable_structure_engine",
    "scanindex.infra",
    "scanindex.infra.file_utils",
    "scanindex.infra.paths",
    "scanindex.infra.translations",
    "scanindex.ui",
    "scanindex.ui.dialogs",
    "scanindex.ui.dialogs.archive_session_dialog",
    "scanindex.ui.dialogs.comparison_dialog",
    "scanindex.ui.dialogs.metadata_dialog",
    "scanindex.ui.dialogs.text_preview_dialog",
    "scanindex.ui.digitization",
    "scanindex.ui.digitization.container",
    "scanindex.ui.digitization.extraction_step",
    "scanindex.ui.digitization.signing_step",
    "scanindex.ui.digitization.split_step",
    "scanindex.ui.icons",
    "scanindex.ui.main_window",
    "scanindex.ui.model_manager",
    "scanindex.ui.pdf_to_word",
    "scanindex.ui.pdf_to_word.view",
    "scanindex.ui.repository",
    "scanindex.ui.repository.screen",
    "scanindex.ui.screens",
    "scanindex.ui.screens.accuracy_screen",
    "scanindex.ui.screens.home_screen",
    "scanindex.ui.screens.kho_luu_tru_screen",
    "scanindex.ui.screens.screen_base",
    "scanindex.ui.screens.secret_file_scan_screen",
    "scanindex.ui.screens.support_tools_screen",
    "scanindex.ui.signals",
    "scanindex.ui.splash_screen",
    "scanindex.ui.tabs",
    "scanindex.ui.tabs.about_tab",
    "scanindex.ui.tabs.dnd_tab",
    "scanindex.ui.tabs.settings_tab",
    "scanindex.ui.theme",
    "scanindex.ui.widgets",
    "scanindex.ui.widgets.file_item_widget",
    "scanindex.ui.widgets.file_list_widget",
    "scanindex.ui.widgets.fuzzy_combobox",
    "scanindex.ui.widgets.kie_archive_viewer",
    "scanindex.ui.widgets.log_panel",
    "scanindex.ui.widgets.pdf_split_viewer",
    "scanindex.ui.widgets.pdf_viewer_widget",
    "scanindex.ui.widgets.section_card",
    "scanindex.ui.widgets.splash_overlay",
    "scanindex.ui.widgets.status_pill",

    # PySide6
    "PySide6",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",

    # Third-party libraries commonly missed by static analysis.
    "PIL",
    "PIL.Image",
    "asn1crypto",
    "cryptography",
    "cv2",
    "docx",
    "fitz",
    "joblib",
    "lightgbm",
    "numpy",
    "onnxruntime",
    "openpyxl",
    "pandas",
    "pikepdf",
    "pyhanko",
    "pyhanko.sign",
    "pyhanko.stamp",
    "pyhanko.pdf_utils",
    "pyhanko_certvalidator",
    "pypdf",
    "tokenizers",
    "rapidfuzz",
    "sentencepiece",
    "tantivy",
    "transformers",
    "transformers.models.layoutlmv3.configuration_layoutlmv3",
    "transformers.models.layoutlmv3.tokenization_layoutlmv3_fast",
    "transformers.models.xlm_roberta.tokenization_xlm_roberta_fast",
    "tzlocal",
    "underthesea",
]


packages_to_collect = [
    # Keep this list narrow. collect_all("PySide6"/"transformers"/"torch")
    # pulls large optional stacks such as Qt3D, torchvision, timm, notebooks,
    # and training utilities that the portable runtime does not use.
    "sentencepiece",
    "onnxruntime",
    "tantivy",
    "underthesea",
    "lightgbm",
    "joblib",
    "pyhanko",
    "pyhanko_certvalidator",
    "asn1crypto",
    "cryptography",
    "tzlocal",
]

if include_correction:
    hiddenimports += [
        "ctranslate2",
        "transformers.models.t5.tokenization_t5_fast",
    ]
    packages_to_collect.append("ctranslate2")

for pkg in packages_to_collect:
    try:
        pkg_datas, pkg_bins, pkg_hidden = collect_all(pkg)
        datas += pkg_datas
        binaries += pkg_bins
        hiddenimports += pkg_hidden
    except Exception:
        pass


excludes = [
    "customtkinter",
    "tkinterdnd2",
    "tkinter",
    "_tkinter",
    "PIL._tkinter_finder",
    "matplotlib",
    "seaborn",
    "plotly",
    "bokeh",
    "statsmodels",
    "scipy",
    "sklearn",
    "pyarrow",
    "numba",
    "llvmlite",
    "timm",
    "torch",
    "faiss",
    "sentence_transformers",
    "selenium",
    "webdriver_manager",
    "torchvision",
    "cupy",
    "dask",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtHttpServer",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtSpatialAudio",
    "PySide6.QtSql",
    "PySide6.QtStateMachine",
    "PySide6.QtSvgWidgets",
    "PySide6.QtTest",
    "PySide6.QtTextToSpeech",
    "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebSockets",
    "PySide6.QtXml",
    "IPython",
    "notebook",
    "jupyter",
    "jupyter_client",
    "jupyter_core",
    "pytest",
    "pylint",
    "flake8",
    "mypy",
    "tensorflow",
    "tensorboard",
    "keras",
    "theano",
    "caffe",
    "mxnet",
    "jax",
    "flax",
    "onnx",
    "nvidia",
    "triton",
    "torchaudio",
    "botocore",
    "boto3",
    "s3transfer",
    "awscli",
    "camelot",
    "ghostscript",
    "curses",
    "pywin.debugger",
    "pywin.framework",
    "pywin.dialogs",
    "win32com",
    "torch._dynamo",
    "torch._numpy",
]

if not include_correction:
    excludes += ["ctranslate2"]


a = Analysis(
    ["ocr_app.py"],
    pathex=[os.getcwd()],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ScanIndex",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon="assets/icon.ico" if os.path.exists("assets/icon.ico") else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=_COLLECT_NAME,
)
