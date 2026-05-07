"""Utility functions for portable executable mode with OFFLINE support"""
import os
import sys
import stat

def ensure_writable(path):
    """
    Ensure a file is writable by removing the Read-Only attribute.
    No-op if file doesn't exist.
    """
    if os.path.exists(path):
        try:
            # Clear Read-Only bit (S_IWRITE is the flag for writable)
            os.chmod(path, stat.S_IWRITE)
        except Exception as e:
            print(f"[Portable] Warning: Failed to set writable permissions for {path}: {e}")


def get_base_dir():
    """
    Get base directory of the application.
    Works for both:
    - Script mode: returns directory containing the script
    - Frozen exe mode: returns directory containing the .exe
    """
    if getattr(sys, 'frozen', False):
        # Running as compiled executable (PyInstaller)
        return os.path.dirname(sys.executable)
    else:
        # Running as Python script
        return os.path.abspath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir)
        )

def get_bundle_dir():
    """
    Return PyInstaller's read-only bundle directory when frozen.
    In one-dir builds this is usually <app>/_internal.
    """
    if getattr(sys, 'frozen', False):
        return getattr(sys, "_MEIPASS", os.path.join(get_base_dir(), "_internal"))
    return get_base_dir()

def get_resource_path(relative_path):
    """
    Get absolute path to a resource.

    Runtime-mutable resources (settings, models, dictionaries) live next to the
    executable. Bundled read-only resources can live under PyInstaller's
    _MEIPASS/_internal directory, so use that as a fallback.
    """
    base_path = os.path.join(get_base_dir(), relative_path)
    if os.path.exists(base_path):
        return base_path
    bundle_path = os.path.join(get_bundle_dir(), relative_path)
    if os.path.exists(bundle_path):
        return bundle_path
    return base_path

def is_frozen():
    """Check if running as frozen executable"""
    return getattr(sys, 'frozen', False)

def setup_offline_mode():
    """
    Configure environment for 100% offline cpu operation.
    MUST be called BEFORE importing transformers, torch, etc.
    """
    base = get_base_dir()
    
    # 1. Force CPU Only
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    
    # 2. Set Transformers to offline mode
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"
    
    # 3. Set HuggingFace cache to bundled locations. Archive search models
    # are the preferred cache root; gmft_models is kept for legacy builds.
    for cache_name in ("archive_models", "gmft_models"):
        hf_cache = os.path.join(base, "models", cache_name)
        if os.path.exists(hf_cache):
            os.environ["TRANSFORMERS_CACHE"] = hf_cache
            os.environ["HF_HOME"] = hf_cache
            os.environ["HUGGINGFACE_HUB_CACHE"] = hf_cache
            print(f"[Portable] Info: Using offline HF cache at {hf_cache}")
            break
    
    # 4. Disable torch hub downloads
    os.environ["TORCH_HOME"] = os.path.join(base, "models", "torch_cache")
    
    # 5. Prevent any network calls
    os.environ["NO_PROXY"] = "*"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    # 6. Ultralytics (DocLayout-YOLO) offline mode
    os.environ["YOLO_OFFLINE"] = "1"
    # Prevent ultralytics from checking for updates or sending analytics
    os.environ["ULTRALYTICS_HUB"] = "0"

    # 7. Mock torch._dynamo to prevent PyInstaller crashes
    # Transformers imports this even in offline mode, causing crashes in frozen builds
    import sys
    from types import ModuleType
    
    # Only mock in the frozen build; dev runs should keep the real torch module.
    if is_frozen() and "torch._dynamo" not in sys.modules:
        print("[Portable] Info: Mocking torch._dynamo for offline compatibility")
        
        # MOCK 1: torch._dynamo
        dynamo_mock = ModuleType("torch._dynamo")
        sys.modules["torch._dynamo"] = dynamo_mock
        
        # MOCK 2: torch._dynamo._trace_wrapped_higher_order_op
        # Required by: transformers/masking_utils.py
        trace_mock = ModuleType("torch._dynamo._trace_wrapped_higher_order_op")
        trace_mock.TransformGetItemToIndex = type("TransformGetItemToIndex", (object,), {})
        sys.modules["torch._dynamo._trace_wrapped_higher_order_op"] = trace_mock
        dynamo_mock._trace_wrapped_higher_order_op = trace_mock

    
    
# Auto-setup when imported in frozen mode
if is_frozen():
    setup_offline_mode()
