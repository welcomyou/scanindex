"""
OFFLINE MODEL DOWNLOADER for Lightweight OCR
============================================
Run this script ONCE on a machine with internet.
It will populate the 'models' and 'drivers' directories.
"""
import os
import sys
import shutil

# Setup relative paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = os.path.join(BASE_DIR, "models")
DRIVERS_DIR = os.path.join(BASE_DIR, "drivers")

def download_chromedriver():
    """Download ChromeDriver to drivers/ folder"""
    print("\n[task] Downloading ChromeDriver...")
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        # WDM installs to cache, we assume latest
        driver_path = ChromeDriverManager().install()
        print(f"  -> Found/Downloaded at: {driver_path}")
        
        # Copy to local drivers/ folder for bundling
        os.makedirs(DRIVERS_DIR, exist_ok=True)
        target_path = os.path.join(DRIVERS_DIR, "chromedriver.exe")
        
        shutil.copy2(driver_path, target_path)
        print(f"  -> Copied to: {target_path}")
        return True
    except Exception as e:
        print(f"  -> Error: {e}")
        return False

def download_doclayout_yolo_model():
    """Download DocLayout-YOLO model for layout analysis"""
    print("\n[task] Downloading DocLayout-YOLO model...")

    target_path = os.path.join(MODELS_DIR, "doclayout_yolo_docstructbench_imgsz1024.pt")
    if os.path.exists(target_path):
        size_mb = os.path.getsize(target_path) / 1024 / 1024
        print(f"  -> Already exists ({size_mb:.1f} MB), skipping.")
        return True

    try:
        from huggingface_hub import hf_hub_download
        path = hf_hub_download(
            repo_id="juliozhao/DocLayout-YOLO-DocStructBench",
            filename="doclayout_yolo_docstructbench_imgsz1024.pt",
            local_dir=MODELS_DIR,
        )
        size_mb = os.path.getsize(path) / 1024 / 1024
        print(f"  -> Downloaded to: {path} ({size_mb:.1f} MB)")
        return True
    except Exception as e:
        print(f"  -> Error: {e}")
        print("  -> (Layout analysis will be disabled without this model)")
        return False


def download_archive_models():
    """Pre-download Kho lưu trữ models so the portable build can run offline.

    E5-small mix50 ONNX must already exist under models/archive_models/.
    """
    print("\n[task] Checking Kho lưu trữ models (Vietnamese embedding ONNX)...")
    target_dir = os.path.join(MODELS_DIR, "archive_models")
    os.makedirs(target_dir, exist_ok=True)
    os.environ["TRANSFORMERS_CACHE"] = target_dir
    os.environ["HF_HOME"] = target_dir

    try:
        onnx_dir = os.path.join(target_dir, "e5-small-mix50-v2-onnx-fp32")
        onnx_model = os.path.join(onnx_dir, "model.onnx")
        if os.path.exists(onnx_model):
            print(f"  -> Embedding ONNX exists: {onnx_model}")
        else:
            print(
                "  -> Missing E5 mix50 ONNX. Copy the trained artifact to "
                f"{onnx_dir} before building a portable offline package."
            )

        print(f"  -> Archive model dir checked: {target_dir}")
        return True
    except Exception as e:
        print(f"  -> Error: {e}")
        print("  -> (Kho lưu trữ search will require internet on first launch.)")
        import traceback
        traceback.print_exc()
        return False


def main():
    print(f"Initializing download to: {BASE_DIR}")

    # 1. Driver
    download_chromedriver()

    # 2. DocLayout-YOLO model
    download_doclayout_yolo_model()

    # 4. Kho lưu trữ models
    download_archive_models()

    print("\nDone! Ready to build.")


if __name__ == "__main__":
    main()
