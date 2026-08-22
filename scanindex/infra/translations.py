
import re
from functools import lru_cache

from scanindex.infra.ui_text_catalog import UI_TEXT_PAIRS
from scanindex.infra.ui_log_text_catalog import UI_LOG_TEXT_PAIRS


# Dictionary structure: Key -> {LangCode -> Text}
# LangCode: "en" (English), "vi" (Vietnamese)

TRANSLATIONS = {
    "app_title": {
        "en": "ScanIndex",
        "vi": "ScanIndex"
    },
    # Tabs
    "tab_dnd": {
        "en": "Drag & Drop",
        "vi": "Kéo thả"
    },
    "tab_batch": {
        "en": "Batch Folder",
        "vi": "Thư Mục"
    },
    "tab_settings": {
        "en": "Settings",
        "vi": "Cài Đặt"
    },
    "tab_about": {
        "en": "About",
        "vi": "Giới Thiệu"
    },
    "txt_about_content": {
        "vi": """ScanIndex
Thiết kế nghiệp vụ: Nguyễn Hồng Quân, Chuyên viên Phòng Chuyển đổi số - Cơ yếu, Văn phòng Thành ủy Thành phố Hồ Chí Minh.
Phát triển và tích hợp: Nguyễn Hồng Quân, với hỗ trợ AI coding.
Phiên bản: {version}
Cập nhật nội dung: 06/05/2026

PHẦN MỀM SỬ DỤNG TRONG MÔI TRƯỜNG GIÁO DỤC, HÀNH CHÍNH CÔNG, TỔ CHỨC ĐẢNG, ĐOÀN THỂ.
KHÔNG SỬ DỤNG CHO MỤC ĐÍCH THƯƠNG MẠI.

MỤC ĐÍCH
ScanIndex hỗ trợ số hóa, nhận dạng, trích xuất metadata, tra cứu và đóng gói tài liệu PDF phục vụ nghiệp vụ lưu trữ.

CHỨC NĂNG CHÍNH
1. Chuyển scan PDF thành PDF có lớp chữ và DOCX:
    • OCR PDF scan bằng ScreenAI chạy trực tiếp qua DLL, xử lý song song theo trang/file.
    • Tự xử lý PDF số: đọc lớp chữ gốc khi PDF đã có text layer.
    • Sinh PDF OCR kèm JSON canonical để phục vụ sửa lỗi, trích xuất và xuất Word.
    • Xuất DOCX có bảng biểu/hình ảnh bằng PyMuPDF + python-docx; DocLayout-YOLO ONNX xác định vùng bảng, GMFT-ONNX và Docling TableFormer v1 ONNX nhận dạng cấu trúc bảng.

2. Sửa lỗi và chuẩn hóa văn bản tiếng Việt:
    • Tùy chọn dùng mô hình Proton Legal TC đóng gói CTranslate2.
    • Bỏ qua sửa lỗi đối với PDF số khi lớp chữ gốc đã đáng tin cậy.

3. Số hóa lưu trữ:
    • Bước 1: phân tách PDF hồ sơ dài thành từng văn bản, có xem trước và cache OCR theo trang.
    • Bước 2: OCR, sửa lỗi, trích xuất KIE/metadata văn bản hành chính; hỗ trợ LightGBM chọn trang ký và LayoutLMv3 runtime cho KIE.
    • Xuất bộ kết quả HSLTCQ: PDF đã xử lý và MetaDuLieu.xlsx theo mẫu chuẩn.

4. Ký số và chuẩn lưu trữ:
    • Ký số hàng loạt PDF qua chứng thư trong Windows Certificate Store.
    • Hỗ trợ mẫu dấu ký, vị trí ký, timestamp TSA và tùy chọn chuyển PDF/A bằng Ghostscript trước khi ký.

5. Kho lưu trữ và tra cứu:
    • Nhập PDF đã số hóa vào Kho nội bộ, lưu SQLite.
    • Tra cứu metadata và toàn văn bằng Tantivy, SQLite và bộ lọc cấu trúc.
    • Xem PDF, đoạn khớp và bộ lọc HSLTCQ ngay trong giao diện.

6. Một số công cụ hỗ trợ:
    • Đo độ chính xác OCR: so sánh PDF OCR với ground truth bằng CER/WER.
    • Phát hiện file mật trong thư mục: OCR trang đầu hoặc các trang đầu văn bản do LightGBM xác định để tìm dấu MẬT/TỐI MẬT/TUYỆT MẬT.

CÔNG NGHỆ CHÍNH
    • Giao diện: Python, PySide6/Qt.
    • PDF/OCR: ScreenAI DLL, PyMuPDF, pypdf/pikepdf, OpenCV.
    • Layout & bảng: DocLayout-YOLO ONNX, GMFT-ONNX, Docling TableFormer v1 ONNX, PyMuPDF, python-docx.
    • AI/KIE/tìm kiếm: CTranslate2, Transformers/ONNX Runtime, LayoutLMv3, LightGBM, Tantivy, SQLite.
    • Ký số: pyHanko, Windows CSP/CNG, TSA, Ghostscript PDF/A.

GHI CHÚ
Phần mềm ưu tiên xử lý cục bộ khi mô hình và phụ thuộc đã được cài đặt. Một số tính năng như tải/cài mô hình, timestamp TSA hoặc cập nhật phụ thuộc có thể cần kết nối mạng theo cấu hình.
""",
        "en": """ScanIndex
Business design: Nguyen Hong Quan, Digital Transformation and Cryptography Specialist, HCMC Party Committee Office.
Development and integration: Nguyen Hong Quan, with AI coding support.
Version: {version}
Content updated: 06 May 2026

THIS SOFTWARE IS INTENDED FOR USE IN EDUCATIONAL ENVIRONMENTS, PUBLIC ADMINISTRATION, PARTY ORGANIZATIONS, AND SOCIO-POLITICAL ORGANIZATIONS.
NOT FOR COMMERCIAL USE.

PURPOSE
ScanIndex supports PDF digitization, OCR, metadata extraction, search, and archival packaging workflows.

MAIN FEATURES
1. Convert scanned PDF to searchable PDF and DOCX:
    • Runs ScreenAI directly through the DLL, with parallel processing by page/file.
    • Handles digital PDFs by reading the existing text layer when available.
    • Produces OCR PDFs and canonical JSON for correction, extraction, and Word export.
    • Exports DOCX with tables/images using PyMuPDF + python-docx; DocLayout-YOLO ONNX detects table regions, while GMFT-ONNX and Docling TableFormer v1 ONNX recognize table structure.

2. Vietnamese text correction and normalization:
    • Optional Proton Legal TC model packaged with CTranslate2.
    • Skips correction for digital PDFs when the native text layer is reliable.

3. Archival digitization:
    • Step 1: split long dossier PDFs into documents, with preview and page-level OCR cache.
    • Step 2: OCR, correction, and KIE/metadata extraction for administrative documents; supports LightGBM signer-page selection and LayoutLMv3 runtime for KIE.
    • Exports HSLTCQ output: processed PDFs and MetaDuLieu.xlsx based on the official template.

4. Digital signing and archival standards:
    • Batch-signs PDFs through certificates in Windows Certificate Store.
    • Supports signature stamp templates, placement, TSA timestamping, and optional Ghostscript PDF/A conversion before signing.

5. Archive repository and search:
    • Imports digitized PDFs into a local SQLite-backed repository.
    • Searches metadata and full text with Tantivy, SQLite, and structured filters.
    • Shows PDFs, matched snippets, and HSLTCQ filters inside the interface.

6. Supporting tools:
    • OCR accuracy measurement: compares OCR PDF output with ground truth using CER/WER metrics.
    • Classified-file detection in a folder: OCRs first pages or LightGBM-selected document-start pages to find MẬT/TỐI MẬT/TUYỆT MẬT stamps.

CORE TECHNOLOGY
    • Interface: Python, PySide6/Qt.
    • PDF/OCR: ScreenAI DLL, PyMuPDF, pypdf/pikepdf, OpenCV.
    • Layout & tables: DocLayout-YOLO ONNX, GMFT-ONNX, Docling TableFormer v1 ONNX, PyMuPDF, python-docx.
    • AI/KIE/search: CTranslate2, Transformers/ONNX Runtime, LayoutLMv3, LightGBM, Tantivy, SQLite.
    • Digital signing: pyHanko, Windows CSP/CNG, TSA, Ghostscript PDF/A.

NOTE
The software prioritizes local processing when models and dependencies are installed. Some operations, such as model installation, TSA timestamping, or dependency updates, may require network access depending on configuration.
"""
    },
    # DND Tab
    "btn_add_files": {
        "en": "+ Add Files",
        "vi": "+ Thêm File"
    },
    "chk_correct": {
        "en": "Correct text",
        "vi": "Sửa lỗi chính tả"
    },
    "chk_correct_enabled": {
        "en": "Enable text correction (load model on demand)",
        "vi": "Bật sửa chính tả (tải model khi cần)"
    },
    "chk_translate_vi": {
        "en": "Translate to Vietnamese",
        "vi": "Dịch sang tiếng Việt"
    },
    "chk_zip_include_canonical": {
        "en": "Include OCR/KIE data (.json.zst) inside exported ZIP",
        "vi": "Kèm dữ liệu OCR/KIE (.json.zst) trong file ZIP xuất ra"
    },
    "tooltip_zip_include_canonical": {
        "en": "When enabled, each exported PDF is bundled with its canonical "
              ".json.zst sidecar (OCR text + KIE annotations) so the ZIP can "
              "be re-imported into the Repository without re-running OCR/KIE. "
              "The receiving PMKhoSohoa importer only scans the .pdf files "
              "and the workbook, so the extra files are ignored there.",
        "vi": "Khi bật, mỗi PDF xuất ra sẽ được kèm file .json.zst cạnh nó "
              "(chữ OCR + kết quả KIE) để file ZIP có thể nhập ngược lại vào "
              "Kho lưu trữ mà không cần chạy lại OCR/KIE. Phần mềm kho chỉ "
              "quét file .pdf và file Excel nên các file kèm theo bị bỏ qua.",
    },
    "chk_skip_duplicate_docs": {
        "en": "Skip duplicate documents when importing into the Repository",
        "vi": "Không lưu văn bản trùng thừa khi nhập vào Kho"
    },
    "tooltip_skip_duplicate_docs": {
        "en": "When enabled (default), a PDF whose content is byte-identical "
              "to a document already in the same dossier is skipped during "
              "import (counted as duplicate). When disabled, identical "
              "copies are still saved — for dossiers that intentionally "
              "contain e.g. the original and a certified copy — and the "
              "import summary reports how many duplicates were kept.",
        "vi": "Khi bật (mặc định), PDF trùng nội dung từng byte với một văn "
              "bản đã có trong cùng hồ sơ sẽ bị bỏ qua khi nhập (đếm là "
              "trùng thừa). Khi tắt, các bản trùng vẫn được lưu — dành cho "
              "hồ sơ có chủ đích chứa cả bản gốc lẫn bản sao y — và tổng "
              "kết sau nhập sẽ báo đã lưu bao nhiêu bản trùng.",
    },
    "tooltip_translate_vi": {
        "en": "Also export a Vietnamese translation as {name}_vi.docx after the Word export completes.",
        "vi": "Sau khi xuất Word, xuất thêm bản dịch tiếng Việt {name}_vi.docx."
    },
    "chk_export": {
        "en": "Word export",
        "vi": "Xuất Word"
    },
    "btn_process_all": {
        "en": "Process All",
        "vi": "Xử lý"
    },
    "btn_stop": {
        "en": "STOP",
        "vi": "Dừng"
    },
    "btn_clear": {
        "en": "Clear",
        "vi": "Xóa"
    },
    # Tooltips
    "tooltip_rerun": {
        "en": "Re-process",
        "vi": "Xử lý lại"
    },
    "tooltip_view_raw": {
        "en": "View raw OCR text",
        "vi": "Xem văn bản OCR thô"
    },
    "tooltip_view_compare": {
        "en": "Compare raw vs corrected OCR texts",
        "vi": "So sánh văn bản OCR thô và chỉnh sửa"
    },
    "tooltip_metadata": {
        "en": "View document metadata",
        "vi": "Xem thông tin văn bản"
    },
    "tooltip_open_output_folder": {
        "en": "Open output folder",
        "vi": "M\u1edf th\u01b0 m\u1ee5c ch\u1ee9a file"
    },
    # Metadata fields
    "chk_metadata": {
        "en": "Extract metadata",
        "vi": "Bóc tách thông tin"
    },
    "lbl_doc_type": {
        "en": "Document System",
        "vi": "Hệ thống VB"
    },
    "lbl_co_quan": {
        "en": "Issuing Authority",
        "vi": "Cơ quan ban hành"
    },
    "lbl_ngay": {
        "en": "Date",
        "vi": "Ngày ban hành"
    },
    "lbl_so_ky_hieu": {
        "en": "Doc Number",
        "vi": "Số, Ký hiệu"
    },
    "lbl_trich_yeu": {
        "en": "Subject",
        "vi": "Trích yếu"
    },
    "lbl_nguoi_ky": {
        "en": "Signer",
        "vi": "Người ký"
    },
    "lbl_loai_vb": {
        "en": "Doc Type",
        "vi": "Loại văn bản"
    },
    "btn_copy_metadata": {
        "en": "Copy All",
        "vi": "Sao chép tất cả"
    },
    # Batch Tab
    "lbl_input_folder": {
        "en": "Input Folder:",
        "vi": "Thư mục đầu vào:"
    },
    "lbl_output_folder": {
        "en": "Output Folder:",
        "vi": "Thư mục đầu ra:"
    },
    "lbl_batch_note": {
        "en": "All .pdf files will be processed.",
        "vi": "Tất cả file .pdf sẽ được xử lý."
    },
    "btn_stop_process": {
        "en": "STOP PROCESS",
        "vi": "Dừng xử lý"
    },
    # Settings Tab
    "lbl_language": {
        "en": "Language:",
        "vi": "Ngôn ngữ:"
    },
    "lbl_theme": {
        "en": "Theme:",
        "vi": "Giao diện:"
    },
    "theme_dark": {
        "en": "Dark",
        "vi": "Tối"
    },
    "theme_light": {
        "en": "Light",
        "vi": "Sáng"
    },
    "msg_theme_restart_required": {
        "en": "Theme will apply after restarting the app.",
        "vi": "Thay đổi giao diện sẽ áp dụng sau khi khởi động lại ứng dụng."
    },
    "lbl_wait_page": {
        "en": "Initial Wait Per Page (seconds):",
        "vi": "Thời gian chờ mỗi trang (giây):"
    },
    "lbl_compare_int": {
        "en": "Comparison Interval (seconds):",
        "vi": "Thời gian chờ so sánh (giây):"
    },
    "lbl_concurrency_ocr": {
        "en": "OCR Files In Parallel:",
        "vi": "Số file OCR cùng lúc:"
    },
    "lbl_concurrency_export": {
        "en": "Word Export Processes:",
        "vi": "Số tiến trình xuất Word:"
    },
    "lbl_settings_desc": {
        "en": "ScreenAI now OCRs each PDF directly in parallel.\n- 1 file uses about 4 internal OCR workers (2 processes x 2 workers).\n- This setting is the number of PDFs to OCR at the same time.\n- On weaker CPUs, keep it at 1.",
        "vi": "ScreenAI hiện OCR trực tiếp theo kiến trúc song song.\n- 1 file thường dùng khoảng 4 worker OCR nội bộ (2 process x 2 worker).\n- Ô này là số file PDF OCR cùng lúc.\n- Máy yếu nên để 1."
    },
    "lbl_correction_model": {
        "en": "Vietnamese Correction Model:",
        "vi": "Mô hình sửa lỗi tiếng Việt:"
    },
    "lbl_acceleration": {
        "en": "AI Acceleration:",
        "vi": "Tăng tốc AI:"
    },
    "lbl_logging": {
        "en": "Logging:",
        "vi": "Ghi nhật ký:"
    },
    "chk_verbose_log": {
        "en": "Enable Verbose/Debug Logs",
        "vi": "Bật log chi tiết/Debug"
    },
    "chk_show_log_panel": {
        "en": "Show Log Panel",
        "vi": "Hiển thị bảng nhật ký"
    },
    "btn_save_settings": {
        "en": "Save Settings Now",
        "vi": "Lưu Cài Đặt Ngay"
    },
    # Right Panel
    "lbl_activity_log": {
        "en": "Activity Log & Progress",
        "vi": "Nhật Ký & Tiến Độ"
    },
    "lbl_chrome_profile_dir": {
        "en": "Chrome Profile Dir (Optional):",
        "vi": "Thư mục Profile Chrome (Tùy chọn):"
    },
    # Messages / Status
    "status_pending": { "en": "Pending", "vi": "Chờ xử lý" },
    "status_processing": { "en": "Processing", "vi": "Đang xử lý" },
    "status_ocr_processing": { "en": "OCR Processing", "vi": "Đang OCR" },
    "status_ocr_done": { "en": "OCR Done", "vi": "OCR Xong" },
    "status_correcting": { "en": "Correcting...", "vi": "Đang sửa lỗi..." },
    "status_corrected": { "en": "Corrected", "vi": "Đã sửa lỗi" },
    "status_exporting": { "en": "Exporting...", "vi": "Đang xuất..." },
    "status_done": { "en": "Done", "vi": "Hoàn tất" },
    "status_failed": { "en": "Failed", "vi": "Thất bại" },
    
    # Dialogs / Logs
    "msg_added_files": {
        "en": "Added {} files manually.",
        "vi": "Đã thêm {} file thủ công."
    },
    "msg_added_drop": {
        "en": "Added {} files via Drop.",
        "vi": "Đã thêm {} file qua kéo thả."
    },
    "msg_process_complete": {
        "en": "Processing Complete! ({}/{} success)",
        "vi": "Xử lý hoàn tất! ({}/{} thành công)"
    },
    "msg_settings_saved": {
        "en": "Settings saved to {}",
        "vi": "Đã lưu cài đặt vào {}"
    },
    "msg_settings_loaded": {
        "en": "Settings loaded from {}",
        "vi": "Đã tải cài đặt từ {}"
    },
    "msg_ocr_lib_error": {
        "en": "Error: {}",
        "vi": "Lỗi: {}"
    },
    "msg_save_success": {
        "en": "Success",
        "vi": "Thành công" 
    },
    "msg_save_content": { 
        "en": "Saved to:\n{}\n\n{}", 
        "vi": "Đã lưu tại:\n{}\n\n{}" 
    },
    "msg_error": {
        "en": "Error",
        "vi": "Lỗi"
    },
    # Window Titles
    "msg_info_init_models": {
        "en": "Initializing AI Models...",
        "vi": "Đang khởi tạo mô hình AI..."
    },
    "msg_info_models_ready": {
        "en": "AI Models Initialized.",
        "vi": "Mô hình AI đã sẵn sàng."
    },
    "win_comparison": { "en": "Comparison: {}", "vi": "So sánh: {}" },
    "win_raw_text": { "en": "Raw OCR Text: {}", "vi": "Văn bản OCR thô: {}" },

    # Settings Section Headers
    "sec_general": { "en": "GENERAL", "vi": "CHUNG" },
    "sec_ocr_processing": { "en": "OCR PROCESSING", "vi": "XỬ LÝ OCR" },
    "sec_correction": { "en": "VIETNAMESE CORRECTION", "vi": "SỬA LỖI TIẾNG VIỆT" },
    "sec_logging": { "en": "LOGGING", "vi": "NHẬT KÝ" },

    # About Tab
    "about_app_name": { "en": "ScanIndex", "vi": "ScanIndex" },
    "about_version": { "en": "Version {version}", "vi": "Phiên bản {version}" },
    "about_notice": {
        "en": "ONLY FOR EDUCATIONAL, PUBLIC ADMINISTRATION,\nPARTY, AND SOCIO-POLITICAL ORGANIZATIONS.\nNOT PERMITTED FOR COMMERCIAL USE.",
        "vi": "PHẦN MỀM SỬ DỤNG TRONG MÔI TRƯỜNG GIÁO DỤC,\nHÀNH CHÍNH CÔNG, TỔ CHỨC ĐẢNG, ĐOÀN THỂ.\nKHÔNG SỬ DỤNG CHO MỤC ĐÍCH THƯƠNG MẠI."
    },

    # Splash
    "splash_loading": { "en": "Initializing AI models...", "vi": "Đang khởi tạo mô hình AI..." },

    # Comparison Window
    "comp_raw_results": { "en": "Raw OCR Results", "vi": "Kết quả OCR thô" },
    "comp_corrected_results": { "en": "Corrected Results", "vi": "Kết quả đã sửa" },
    "comp_correction_words": { "en": "Correction Words", "vi": "Từ đã sửa" },
    "comp_active_corrections": { "en": "Active Corrections", "vi": "Sửa đang áp dụng" },
    "comp_restore_candidates": { "en": "Restore Candidates", "vi": "Khôi phục gốc" },
    "comp_reprocess": { "en": "Reprocess", "vi": "Xử lý lại" },
    "comp_stop": { "en": "Stop", "vi": "Dừng" },

    # Treeview Headers
    "tree_original": { "en": "Original", "vi": "Từ gốc" },
    "tree_corrected": { "en": "Corrected", "vi": "Từ sửa" },
    "tree_action": { "en": "Action", "vi": "Thao tác" },

    # Batch count
    "batch_total": { "en": "Total: {} files", "vi": "Tổng: {} file" },

    # ===== Archive Tab =====
    "tab_archive": { "en": "Digital Archiving", "vi": "Số hóa lưu trữ" },
    "arc_input_folder": { "en": "Input folder:", "vi": "Thư mục đầu vào:" },
    "arc_output_folder": { "en": "Output folder:", "vi": "Thư mục đầu ra:" },
    "arc_btn_process": { "en": "Process", "vi": "Xử lý" },
    "arc_btn_stop": { "en": "Stop", "vi": "Dừng" },
    "arc_progress": { "en": "Processing {0}/{1}...", "vi": "Đang xử lý {0}/{1}..." },
    "arc_done": { "en": "Done: {0} files processed", "vi": "Hoàn tất: {0} file đã xử lý" },
    "arc_doc_list": { "en": "Documents (drag to reorder)", "vi": "Tài liệu (kéo thả để sắp thứ tự)" },
    "arc_no_docs": {
        "en": "No documents yet.\nSelect an input folder and click Process.",
        "vi": "Chưa có tài liệu.\nChọn thư mục đầu vào và nhấn Xử lý."
    },
    "arc_field_co_quan": { "en": "Issuing authority", "vi": "Cơ quan ban hành" },
    "arc_field_loai_vb": { "en": "Document type", "vi": "Tên loại văn bản" },
    "arc_field_so": { "en": "Number", "vi": "Số văn bản" },
    "arc_field_ky_hieu": { "en": "Symbol", "vi": "Ký hiệu" },
    "arc_field_ngay": { "en": "Date", "vi": "Ngày tháng năm" },
    "arc_field_trich_yeu": { "en": "Subject", "vi": "Trích yếu nội dung" },
    "arc_field_ngon_ngu": { "en": "Language", "vi": "Ngôn ngữ" },
    "arc_field_nguoi_ky": { "en": "Signer", "vi": "Người ký" },
    "arc_field_do_mat": { "en": "Secrecy", "vi": "Độ mật" },
    "arc_field_trang_so": { "en": "Page no.", "vi": "Trang số" },
    "arc_field_so_trang": { "en": "Pages", "vi": "Số trang" },
    "arc_field_so_thu_tu": { "en": "Ordinal", "vi": "Số thứ tự" },
    "arc_raw_kie_title": { "en": "Raw KIE", "vi": "Thông tin thô (Raw KIE)" },
    "arc_viewer_hint": {
        "en": "Click a field on the left to view its location in the document.",
        "vi": "Nhấn vào trường bên trái để xem vị trí trên tài liệu."
    },
    "arc_page_label": { "en": "Page {0}/{1}", "vi": "Trang {0}/{1}" },
    "arc_no_preview": {
        "en": "Select a document to preview.",
        "vi": "Chọn tài liệu để xem trước."
    },
    "arc_export_csv": { "en": "Export ZIP", "vi": "Xuất hồ sơ nén" },
    "arc_metadata_title": { "en": "Metadata", "vi": "Thông tin" },
    "arc_btn_save": { "en": "Save", "vi": "Lưu" },
    "arc_saved_notice": { "en": "Saved", "vi": "Đã lưu" },

    # ── Step bar ────────────────────────────────────────────────────
    "arc_step1_title": { "en": "Step 1 - Split large file", "vi": "Bước 1 - Tách file lớn" },
    "arc_step2_title": { "en": "Step 2 — Extract KIE", "vi": "Bước 2 — Trích xuất KIE" },
    "arc_step3_title": { "en": "Step 3 — Sign", "vi": "Bước 3 — Ký số" },

    # ── Dossier info dialog (Bước 1 / Kho edit) ─────────────────────
    "arc_session_dialog_title": {
        "en": "Dossier information",
        "vi": "Nhập thông tin hồ sơ",
    },
    "arc_session_dialog_heading": {
        "en": "Dossier identity",
        "vi": "Thông tin hồ sơ",
    },
    "arc_session_dialog_hint": {
        "en": "Output filename: <Identity>-<Fonds>-<Catalog>-<File>-<NNN>.pdf",
        "vi": "Tên file đầu ra sẽ có dạng: <Mã định danh>-<Mã phông>-<Số mục lục>-<Số hồ sơ>-<Số thứ tự trong hồ sơ>.pdf",
    },
    "arc_unstructured_label": {
        "en": "Don't store under this archive code structure",
        "vi": "Không lưu theo cấu trúc này",
    },
    "arc_unstructured_hint": {
        "en": "When checked, archive codes are auto-generated; only the dossier name is required.",
        "vi": "Khi tick, các mã sẽ được tự động sinh; chỉ cần nhập Tên hồ sơ.",
    },
    "arc_field_ma_dd":     { "en": "Identity code", "vi": "Mã định danh" },
    "arc_field_ma_phong":  { "en": "Fonds code",    "vi": "Mã phông" },
    "arc_field_muc_luc":   { "en": "Catalog",       "vi": "Số mục lục" },
    "arc_field_ho_so":     { "en": "File number",   "vi": "Số hồ sơ" },
    "arc_field_title":     { "en": "Dossier name",  "vi": "Tên hồ sơ" },
    "arc_ph_ma_dd":        { "en": "E.g. A29.123",   "vi": "VD: A29.123" },
    "arc_ph_ma_phong":     {
        "en": "E.g. 001, A29.123 when no fonds is available, or A29.123.001 when combined",
        "vi": "VD: 001 hoặc A29.123 nếu không có phông hoặc A29.123.001 nếu kết hợp",
    },
    "arc_ph_muc_luc":      { "en": "E.g. 01 — enter 2 digits", "vi": "VD: 01 - nhập 2 chữ số" },
    "arc_ph_ho_so":        { "en": "E.g. 0001 or 0001a — enter 4 digits", "vi": "VD: 0001 hoặc 0001a - nhập 4 chữ số" },
    "arc_ph_title":        { "en": "Dossier name (maximum 1000 characters)", "vi": "Tên hồ sơ (tối đa 1000 ký tự)" },
    "arc_err_ma_dd_empty":     { "en": "Identity code is required", "vi": "Mã định danh không được để trống" },
    "arc_err_ma_phong_empty":  { "en": "Fonds code is required",    "vi": "Mã phông không được để trống" },
    "arc_err_muc_luc_format":  { "en": "Catalog must be ≤2 chars",  "vi": "Số mục lục tối đa 2 ký tự" },
    "arc_err_ho_so_format":    { "en": "File must be ≤5 chars",     "vi": "Số hồ sơ tối đa 5 ký tự" },
    "arc_err_title_required":  { "en": "Dossier name is required when unstructured",
                                   "vi": "Phải nhập Tên hồ sơ khi không lưu theo cấu trúc" },
    "btn_ok": { "en": "OK", "vi": "OK" },
    "btn_cancel": { "en": "Cancel", "vi": "Huỷ" },

    # ── Step 1 — split UI ───────────────────────────────────────────
    "arc_step1_pick_pdf": { "en": "Choose PDF", "vi": "Chọn PDF" },
    "arc_step1_pick_pdf_title": { "en": "Choose a long PDF to split", "vi": "Chọn file PDF dài cần phân tách" },
    "arc_step1_drop_hint": {
        "en": "Drop or pick a long PDF. Skip Step 1 if you import a folder of files.",
        "vi": "Kéo thả hoặc Chọn PDF dài. Nếu xử lý theo danh sách file trong thư mục, hãy bỏ qua bước 1.",
    },
    "arc_step1_reset": { "en": "Reset", "vi": "Làm lại" },
    "arc_step1_reset_confirm": {
        "en": "Discard the current PDF, all cuts, and any pending background OCR?",
        "vi": "Bỏ file PDF hiện tại, tất cả vị trí cắt, và OCR ngầm đang chạy?",
    },
    "arc_step1_to_step2": { "en": "Go to Step 2 →", "vi": "Chuyển bước 2 →" },
    "arc_step1_to_step2_confirm": {
        "en": "Split into {n} files and continue to Step 2?",
        "vi": "Sẽ tách thành {n} file và chuyển sang Bước 2 — tiếp tục?",
    },
    "arc_step1_segments": { "en": "Segments", "vi": "Văn bản" },
    "arc_confirm_title": { "en": "Confirm", "vi": "Xác nhận" },
    "arc_error_title": { "en": "Error", "vi": "Lỗi" },
    "arc_workflow_reset": { "en": "↻ Start over", "vi": "↻ Bắt đầu lại" },
    "arc_workflow_reset_title": { "en": "Reset workflow?", "vi": "Bắt đầu lại từ đầu?" },
    "arc_workflow_reset_confirm": {
        "en": "Cancel any running task, delete all temp files, and reset every step?",
        "vi": "Hủy mọi tác vụ đang chạy, xóa toàn bộ file tạm và đặt lại tất cả các bước?",
    },

    # ── Step 2 — source mode + warnings ─────────────────────────────
    "arc_step2_source_folder": { "en": "Input folder:", "vi": "Thư mục đầu vào:" },
    "arc_step2_source_folder_hint": {
        "en": "If you skip Step 1 split, choose a folder containing PDFs and click Process",
        "vi": "Nếu bỏ qua bước 1 tách file lớn, vui lòng chọn thư mục chứa pdf và bấm Xử lý",
    },
    "arc_step2_source_step1": { "en": "Source:", "vi": "Nguồn:" },
    "arc_step2_source_step1_hint": { "en": "Files received from Step 1", "vi": "File chuyển từ Bước 1" },
    "arc_step2_source_step1_value": {
        "en": "From Step 1 ({n} files)",
        "vi": "Từ Bước 1 ({n} file)",
    },

    # ── Step 2 — reopen exported ZIP to edit ────────────────────────
    "arc_step2_open_zip": { "en": "Open ZIP", "vi": "Mở ZIP" },
    "arc_step2_open_zip_title": { "en": "Select an exported ZIP", "vi": "Chọn file ZIP đã xuất" },
    "arc_step2_zip_replace_confirm": {
        "en": "A dossier is already loaded. Replace it with the ZIP contents? Unsaved edits will be lost.",
        "vi": "Đang có hồ sơ trong Bước 2. Thay thế bằng nội dung của file ZIP này? Các chỉnh sửa chưa lưu sẽ bị mất.",
    },
    "arc_step2_zip_loaded": {
        "en": "Loaded ZIP — {n} document(s) ready to edit.",
        "vi": "Đã nạp ZIP — {n} văn bản sẵn sàng để chỉnh sửa.",
    },
    "arc_step2_zip_invalid": {
        "en": "Not a valid archive ZIP (missing MetaDuLieu.xlsx under HSLTCQ/METADATA/).",
        "vi": "Không phải file ZIP hồ sơ hợp lệ (thiếu MetaDuLieu.xlsx trong HSLTCQ/METADATA/).",
    },
    "arc_step2_zip_failed": {
        "en": "Failed to read ZIP: {0}",
        "vi": "Không đọc được file ZIP: {0}",
    },
    "arc_step2_reorder_warn_title": {
        "en": "Reorder will reset numbering",
        "vi": "Kéo thả sẽ đánh lại số thứ tự",
    },
    "arc_step2_reorder_warn_body": {
        "en": "The current document numbering is not a complete 1, 2, 3… "
              "sequence (a slot was skipped). Dragging will renumber every "
              "document sequentially (1, 2, 3…). Please verify the correct "
              "order of each document before confirming.",
        "vi": "Số thứ tự hiện tại của các văn bản chưa đủ liên tiếp "
              "(đã bỏ một vị trí). Việc kéo thả sẽ đánh lại số thứ tự tuần tự "
              "(1, 2, 3…). Vui lòng kiểm tra chính xác thứ tự của từng văn bản "
              "trước khi xác nhận.",
    },

    # ── Step 3 placeholder ──────────────────────────────────────────
    "arc_step3_placeholder": {
        "en": "Bulk digital signing — coming soon.",
        "vi": "Tính năng ký số hàng loạt — đang phát triển.",
    },
}

class Localization:
    def __init__(self, lang="en"):
        self.lang = lang

    def set_language(self, lang):
        self.lang = lang if lang in {"en", "vi"} else "en"

    def get(self, key, *args):
        # Default to key if not found
        if key not in TRANSLATIONS:
            return key
        
        val_map = TRANSLATIONS[key]
        # Default to English if lang not found in map
        text = val_map.get(self.lang, val_map.get("en", key))
        
        if args:
            try:
                text = text.format(*args)
            except: pass
        return text

# Global instance
current_locale = Localization("en")

def get_text(key, *args):
    return current_locale.get(key, *args)

def set_lang(lang):
    current_locale.set_language(lang)


# ---------------------------------------------------------------------------
# Source-text localization for legacy/new PySide screens
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")


def _normalise_ui_text(value: str) -> str:
    """Normalize layout whitespace without changing punctuation or accents."""
    return " ".join(str(value or "").split())


def _all_ui_pairs():
    """Yield keyed translations and source-catalog pairs exactly once."""
    seen = set()
    # Source catalog comes first because it gives legacy hard-coded words
    # their UI-context meaning (for example, source "Xóa" means Delete,
    # while the keyed PDF toolbar deliberately uses the same Vietnamese word
    # for Clear and is refreshed explicitly by its update_texts method).
    for en, vi in (*UI_TEXT_PAIRS, *UI_LOG_TEXT_PAIRS):
        marker = (str(en), str(vi))
        if marker[0] and marker[1] and marker not in seen:
            seen.add(marker)
            yield marker
    for values in TRANSLATIONS.values():
        if not isinstance(values, dict):
            continue
        en = str(values.get("en", ""))
        vi = str(values.get("vi", ""))
        marker = (en, vi)
        if en and vi and marker not in seen:
            seen.add(marker)
            yield marker


def _template_regex(template: str):
    """Compile a source template and return it with its capture count."""
    template = str(template)
    matches = list(_PLACEHOLDER_RE.finditer(template))
    if not matches:
        return None, 0

    def literal_pattern(value: str) -> str:
        # UI messages are often assembled with line breaks while their
        # catalog form is kept on one line. Match whitespace flexibly, but
        # capture placeholder values from the original string so filenames
        # and multi-line previews are not flattened during translation.
        return "".join(
            r"\s+" if part.isspace() else re.escape(part)
            for part in re.split(r"(\s+)", value)
            if part
        )

    pieces = []
    cursor = 0
    for match in matches:
        pieces.append(literal_pattern(template[cursor:match.start()]))
        pieces.append("(.*?)")
        cursor = match.end()
    pieces.append(literal_pattern(template[cursor:]))
    return re.compile("^" + "".join(pieces) + "$", re.DOTALL), len(matches)


def _fill_template(template: str, values: tuple[str, ...]) -> str:
    iterator = iter(values)
    return _PLACEHOLDER_RE.sub(lambda _match: next(iterator, ""), template)


def _build_ui_indexes():
    exact = {"en": {}, "vi": {}}
    templates = {"en": [], "vi": []}
    for en, vi in _all_ui_pairs():
        en_key = _normalise_ui_text(en)
        vi_key = _normalise_ui_text(vi)
        en_pattern, en_fields = _template_regex(en)
        vi_pattern, vi_fields = _template_regex(vi)
        if en_pattern or vi_pattern:
            # Only valid pairs can be rendered safely in both directions.
            if en_fields == vi_fields and en_pattern and vi_pattern:
                templates["en"].append((vi_pattern, en))
                templates["vi"].append((en_pattern, vi))
            continue
        exact["en"].setdefault(vi_key, en)
        exact["vi"].setdefault(en_key, vi)
    # Match more specific templates first so a short generic pattern cannot
    # consume text intended for a longer message.
    templates["en"].sort(key=lambda item: len(item[0].pattern), reverse=True)
    templates["vi"].sort(key=lambda item: len(item[0].pattern), reverse=True)
    return exact, templates


_UI_EXACT, _UI_TEMPLATES = _build_ui_indexes()


@lru_cache(maxsize=8192)
def _localize_cached(value: str, target: str) -> str:
    normalised = _normalise_ui_text(value)
    translated = _UI_EXACT[target].get(normalised)
    if translated is not None:
        return translated
    for pattern, output_template in _UI_TEMPLATES[target]:
        match = pattern.fullmatch(value)
        if match:
            return _fill_template(output_template, match.groups())
    return value


def localize_text(text, lang: str | None = None):
    """Translate known UI source text while leaving user/document data alone.

    Exact pairs are preferred. Template pairs support runtime values in
    progress labels and dialog messages. Unknown input is returned unchanged.
    Results are cached because Qt may repaint an unchanged label frequently.
    """
    if text is None:
        return text
    value = str(text)
    target = lang if lang in {"en", "vi"} else current_locale.lang
    return _localize_cached(value, target)


def format_import_summary(
    *, dossiers: int, imported: int, duplicates: int, failed: int,
    duplicate_skipped: int = 0, duplicate_kept: int = 0,
) -> str:
    """Build a localized repository-import summary from atomic UI lines."""
    duplicate_line = localize_text(f"Duplicates: {duplicates}")
    if duplicate_kept:
        duplicate_line += " " + localize_text(
            f"(skipped {duplicate_skipped}, kept {duplicate_kept})"
        )
    return "\n".join((
        localize_text(f"Dossiers: {dossiers}"),
        localize_text(f"Documents imported: {imported}"),
        duplicate_line,
        localize_text(f"Errors: {failed}"),
        "",
        localize_text("You can search immediately."),
    ))


_DOCUMENT_TYPE_EN = {
    "Nghị quyết": "Resolution",
    "Quyết định": "Decision",
    "Chỉ thị": "Directive",
    "Kết luận": "Conclusion",
    "Quy chế": "Regulation",
    "Quy định": "Rules",
    "Thông cáo": "Press release",
    "Thông báo": "Notice",
    "Hướng dẫn": "Guidance",
    "Chương trình": "Program",
    "Kế hoạch": "Plan",
    "Phương án": "Implementation plan",
    "Đề án": "Scheme",
    "Dự án": "Project",
    "Báo cáo": "Report",
    "Biên bản": "Minutes",
    "Tờ trình": "Submission",
    "Hợp đồng": "Contract",
    "Công điện": "Official telegram",
    "Bản ghi nhớ": "Memorandum",
    "Bản thỏa thuận": "Agreement",
    "Giấy ủy quyền": "Power of attorney",
    "Giấy mời": "Invitation",
    "Giấy giới thiệu": "Letter of introduction",
    "Giấy nghỉ phép": "Leave request",
    "Phiếu gửi": "Dispatch slip",
    "Phiếu chuyển": "Transfer slip",
    "Phiếu báo": "Notification slip",
    "Bản sao y": "Certified true copy",
    "Bản trích sao": "Extract copy",
    "Bản sao lục": "Duplicate copy",
    "Công văn": "Official dispatch",
    "Khác": "Other",
}


def localize_document_type(value) -> str:
    """Translate a built-in document type without guessing user catalog data."""
    canonical = str(value or "")
    if current_locale.lang == "en":
        return _DOCUMENT_TYPE_EN.get(canonical, canonical)
    # A translated built-in label may be supplied while switching back.
    reverse = {en: vi for vi, en in _DOCUMENT_TYPE_EN.items()}
    return reverse.get(canonical, canonical)


def add_localized_combo_items(combo, values, *, context: str = "") -> None:
    """Add translated combo labels while retaining each canonical value.

    Business forms historically read ``currentText()`` and therefore tied
    presentation to persisted metadata.  Keeping the canonical value in
    ``Qt.UserRole`` lets callers display English without changing the value
    written to an archive or used in a repository query.
    """
    combo.setProperty("_scanindex_i18n_items", True)
    combo.setProperty("_scanindex_i18n_context", context)
    for value in values:
        canonical = str(value)
        display = (
            localize_document_type(canonical)
            if context == "document_type"
            else localize_text(canonical)
        )
        combo.addItem(display, canonical)


def combo_value(combo) -> str:
    """Return a combo's canonical value, or typed text for editable combos."""
    try:
        index = combo.currentIndex()
        if index >= 0:
            data = combo.itemData(index)
            if data is not None:
                return str(data)
        return str(combo.currentText())
    except Exception:
        return ""


def set_combo_value(combo, value) -> None:
    """Select a canonical value without depending on its translated label."""
    canonical = str(value or "")
    try:
        index = combo.findData(canonical)
        if index >= 0:
            combo.setCurrentIndex(index)
            return
        combo.setCurrentText(canonical)
    except Exception:
        pass


def _localize_item_text(source: str, context: str = "") -> str:
    if context == "secret_note":
        return "; ".join(localize_text(part) for part in source.split("; "))
    return localize_text(source)


def set_translatable_item_text(
    item, source_text, *, context: str = "", sync_tooltip: bool = False,
) -> None:
    """Set a table/tree item's UI text while retaining its canonical source."""
    try:
        from PySide6.QtCore import Qt
        role = int(Qt.ItemDataRole.UserRole) + 197
        source = str(source_text or "")
        item.setData(role, source)
        item.setData(role + 1, context)
        item.setData(role + 2, bool(sync_tooltip))
        translated = _localize_item_text(source, context)
        item.setText(translated)
        if sync_tooltip:
            item.setToolTip(translated)
    except Exception:
        pass


def retranslate_widget_tree(root, *, include_children: bool = True) -> None:
    """Apply the selected language to translatable properties below *root*.

    The function intentionally translates only strings present in the source
    catalog.  Filenames, OCR text, metadata, and other user/document content
    therefore pass through unchanged even when they live in the same widgets.
    """
    if root is None:
        return
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QAction
        from PySide6.QtWidgets import (
            QAbstractButton, QComboBox, QDialogButtonBox, QGroupBox,
            QInputDialog, QLabel, QDoubleSpinBox, QLineEdit, QListWidget,
            QMessageBox, QPlainTextEdit, QProgressDialog, QSpinBox,
            QTabWidget, QTableWidget, QTextEdit, QTreeWidget, QWidget,
        )
    except Exception:
        return

    objects = [root]
    if include_children:
        try:
            objects.extend(root.findChildren(QWidget))
            objects.extend(root.findChildren(QAction))
        except Exception:
            pass

    def apply_property(obj, getter_name: str, setter_name: str) -> None:
        getter = getattr(obj, getter_name, None)
        setter = getattr(obj, setter_name, None)
        if not callable(getter) or not callable(setter):
            return
        try:
            current = getter()
            updated = localize_text(current)
            if updated != current:
                setter(updated)
        except Exception:
            pass

    for obj in objects:
        if isinstance(obj, QWidget) and obj.property("_scanindex_i18n_skip"):
            continue
        if isinstance(obj, (QLabel, QAbstractButton, QAction, QMessageBox)):
            apply_property(obj, "text", "setText")
        if isinstance(obj, QMessageBox):
            apply_property(obj, "informativeText", "setInformativeText")
            apply_property(obj, "detailedText", "setDetailedText")
            standard_sources = (
                (QMessageBox.StandardButton.Ok, "OK"),
                (QMessageBox.StandardButton.Save, "Save"),
                (QMessageBox.StandardButton.Discard, "Discard"),
                (QMessageBox.StandardButton.Cancel, "Cancel"),
                (QMessageBox.StandardButton.Close, "Close"),
                (QMessageBox.StandardButton.Yes, "Yes"),
                (QMessageBox.StandardButton.No, "No"),
                (QMessageBox.StandardButton.Retry, "Retry"),
                (QMessageBox.StandardButton.Apply, "Apply"),
                (QMessageBox.StandardButton.Reset, "Reset"),
                (QMessageBox.StandardButton.RestoreDefaults,
                 "Restore defaults"),
            )
            for standard, source in standard_sources:
                button = obj.button(standard)
                if button is not None:
                    button.setText(localize_text(source))
        if isinstance(obj, QDialogButtonBox):
            standard_sources = (
                (QDialogButtonBox.StandardButton.Ok, "OK"),
                (QDialogButtonBox.StandardButton.Save, "Save"),
                (QDialogButtonBox.StandardButton.Discard, "Discard"),
                (QDialogButtonBox.StandardButton.Cancel, "Cancel"),
                (QDialogButtonBox.StandardButton.Close, "Close"),
                (QDialogButtonBox.StandardButton.Yes, "Yes"),
                (QDialogButtonBox.StandardButton.No, "No"),
                (QDialogButtonBox.StandardButton.Retry, "Retry"),
                (QDialogButtonBox.StandardButton.Apply, "Apply"),
                (QDialogButtonBox.StandardButton.Reset, "Reset"),
                (QDialogButtonBox.StandardButton.RestoreDefaults,
                 "Restore defaults"),
            )
            for standard, source in standard_sources:
                button = obj.button(standard)
                if button is not None:
                    button.setText(localize_text(source))
        if isinstance(obj, QGroupBox):
            apply_property(obj, "title", "setTitle")
        if isinstance(obj, (QLineEdit, QTextEdit, QPlainTextEdit)):
            apply_property(obj, "placeholderText", "setPlaceholderText")
        if isinstance(obj, (QSpinBox, QDoubleSpinBox)):
            apply_property(obj, "prefix", "setPrefix")
            apply_property(obj, "suffix", "setSuffix")
        if isinstance(obj, QProgressDialog):
            apply_property(obj, "labelText", "setLabelText")
        if isinstance(obj, QInputDialog):
            apply_property(obj, "labelText", "setLabelText")
        if isinstance(obj, QWidget):
            apply_property(obj, "windowTitle", "setWindowTitle")
            apply_property(obj, "toolTip", "setToolTip")
            apply_property(obj, "statusTip", "setStatusTip")
            apply_property(obj, "whatsThis", "setWhatsThis")
        if isinstance(obj, QComboBox):
            for index in range(obj.count()):
                # Only translate items with a separate canonical value. This
                # prevents display translation from mutating model names,
                # certificate names, filenames, and other user data.
                if obj.itemData(index, Qt.ItemDataRole.UserRole) is None:
                    continue
                current = obj.itemText(index)
                if obj.property("_scanindex_i18n_items"):
                    canonical = obj.itemData(index, Qt.ItemDataRole.UserRole)
                    if obj.property("_scanindex_i18n_context") == "document_type":
                        updated = localize_document_type(canonical)
                    else:
                        updated = localize_text(canonical)
                else:
                    updated = localize_text(current)
                if updated != current:
                    obj.setItemText(index, updated)
        if isinstance(obj, QTabWidget):
            for index in range(obj.count()):
                current = obj.tabText(index)
                updated = localize_text(current)
                if updated != current:
                    obj.setTabText(index, updated)
        if isinstance(obj, QTableWidget):
            for col in range(obj.columnCount()):
                item = obj.horizontalHeaderItem(col)
                if item is not None:
                    item.setText(localize_text(item.text()))
            for row in range(obj.rowCount()):
                for col in range(obj.columnCount()):
                    item = obj.item(row, col)
                    if item is None:
                        continue
                    source = item.data(int(Qt.ItemDataRole.UserRole) + 197)
                    if source is not None:
                        context = item.data(
                            int(Qt.ItemDataRole.UserRole) + 198
                        ) or ""
                        translated = _localize_item_text(
                            str(source), str(context)
                        )
                        item.setText(translated)
                        if item.data(int(Qt.ItemDataRole.UserRole) + 199):
                            item.setToolTip(translated)
            for row in range(obj.rowCount()):
                item = obj.verticalHeaderItem(row)
                if item is not None:
                    item.setText(localize_text(item.text()))
        if isinstance(obj, QTreeWidget):
            header = obj.headerItem()
            if header is not None:
                for col in range(header.columnCount()):
                    header.setText(col, localize_text(header.text(col)))


def install_event_filter(app) -> None:
    """Translate newly shown dialogs/widgets, including static QMessageBoxes."""
    try:
        from PySide6.QtCore import QEvent, QObject
        from PySide6.QtWidgets import (
            QListWidget, QTableWidget, QTreeWidget, QWidget,
        )
    except Exception:
        return
    if getattr(app, "_scanindex_i18n_filter", None) is not None:
        return

    class _UiTranslationFilter(QObject):
        def __init__(self, parent=None):
            super().__init__(parent)
            self._busy = False

        def eventFilter(self, watched, event):
            event_type = event.type()
            if (not self._busy and isinstance(watched, QWidget)
                    and event_type in (
                        QEvent.Type.Show,
                        QEvent.Type.Polish,
                        QEvent.Type.Paint,
                    )):
                self._busy = True
                try:
                    # Show/Polish translates the complete new dialog or
                    # screen. Paint handles labels/statuses whose source text
                    # changes later while the widget is already visible.
                    retranslate_widget_tree(
                        watched,
                        include_children=event_type != QEvent.Type.Paint,
                    )
                    # Item views paint their contents on a viewport child;
                    # translate their model/header items through the parent.
                    if event_type == QEvent.Type.Paint:
                        parent = watched.parentWidget()
                        if parent is not None and isinstance(
                            parent, (QTableWidget, QListWidget, QTreeWidget)
                        ):
                            retranslate_widget_tree(
                                parent, include_children=False
                            )
                finally:
                    self._busy = False
            return False

    event_filter = _UiTranslationFilter(app)
    app.installEventFilter(event_filter)
    app._scanindex_i18n_filter = event_filter
