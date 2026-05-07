import os
import shutil
import pypdf
import time
import json
import threading
import sys
import http.server
import socketserver
import socket
import urllib.parse
import tempfile
import glob
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from webdriver_manager.chrome import ChromeDriverManager
import logging
from scanindex.infra import paths as portable_utils
from scanindex.infra.paths import get_base_dir, get_resource_path, is_frozen
from selenium.common.exceptions import SessionNotCreatedException

# Config
BASE_DIR = get_base_dir()
# USE TEMP DIR FOR PROFILE TO AVOID CRASHES ON D: DRIVE
TEMP_DIR = tempfile.gettempdir()
CHROME_DATA_DIR = os.path.join(TEMP_DIR, "ocr_chrome_data")

def get_chromedriver_path(ignore_bundled=False):
    """
    Tries to download the latest driver.
    If it fails (offline/service unavailable), tries to find an existing driver in cache.
    """
    try:
        # 0. Check bundled driver (Portable Mode Priority)
        # If ignore_bundled is True, we skip this to force WDM resolution (useful for fallback)
        if not ignore_bundled:
            bundled_driver = os.path.join(BASE_DIR, "drivers", "chromedriver.exe")
            if os.path.exists(bundled_driver):
                logging.info(f"Using bundled ChromeDriver: {bundled_driver}")
                return bundled_driver

        # 1. Try Online Install (Dev Mode)
        logging.info("Checking for ChromeDriver updates...")
        return ChromeDriverManager().install()
    except Exception as e:
        print(f"Warning: Online ChromeDriver check failed ({e}). Attempting offline fallback...")
        
        # 2. Fallback: Search for existing driver in WDM cache
        # WDM cache is usually in ~/.wdm/drivers/chromedriver/...
        # or bundled in a 'drivers' folder in the future
        
        # Method A: Try to find any chromedriver.exe in ~/.wdm
        user_home = os.path.expanduser("~")
        wdm_root = os.path.join(user_home, ".wdm")
        
        candidates = []
        if os.path.exists(wdm_root):
            for root, dirs, files in os.walk(wdm_root):
                if "chromedriver.exe" in files:
                    candidates.append(os.path.join(root, "chromedriver.exe"))
        
        # Sort by modification time (newest first)
        candidates.sort(key=os.path.getmtime, reverse=True)
        
        if candidates:
            print(f"Offline Fallback: Found existing driver at {candidates[0]}")
            return candidates[0]
            
        if is_frozen():
             raise Exception("ChromeDriver missing from 'drivers/chromedriver.exe'. Please reinstall or contact support.")
            
        raise Exception("ChromeDriver not found and cannot install (No Internet?). Please run online at least once.")

# Master Template Source (Bundled/Dev)
MASTER_TEMPLATE_NAME = "ocr_chrome_userdata_ori"
SOURCE_TEMPLATE_DIR = os.path.join(BASE_DIR, "models", MASTER_TEMPLATE_NAME)

# Dest Master Profile in Temp
DEST_MASTER_DIR = os.path.join(TEMP_DIR, "ocr_chrome_data")

# Global Lock for Profile Operations to prevent Race Conditions
_PROFILE_LOCK = threading.Lock()

def ensure_master_profile():
    """
    Ensures the Master Profile exists in TEMP.
    If not, copies from SOURCE_TEMPLATE_DIR.
    Thread-safe to prevent multiple threads corrupting the profile.
    """
    with _PROFILE_LOCK:
        # 1. Check if Master already exists and looks valid (simple check)
        # We check for 'Local State' as a marker
        if os.path.exists(os.path.join(DEST_MASTER_DIR, "Local State")):
            return True

        # 2. Copy from Source Template
        if os.path.exists(SOURCE_TEMPLATE_DIR):
            try:
                print(f"Initializing Master Profile from: {SOURCE_TEMPLATE_DIR}")
                if os.path.exists(DEST_MASTER_DIR): shutil.rmtree(DEST_MASTER_DIR)
                shutil.copytree(SOURCE_TEMPLATE_DIR, DEST_MASTER_DIR)
                return True
            except Exception as e:
                print(f"Error copying Master Profile: {e}")
                return False
        else:
            # If Source is missing (Dev mode without setup?), we fall back to creating empty
            # But User requested we use the "ori" folder.
            print(f"Warning: Master Template '{MASTER_TEMPLATE_NAME}' not found in models.")
            # We allow proceeding, Chrome will create fresh, but might miss ScreenAI if it was inside
            return True

def setup_library():
    """Legacy Name - Now just ensures Master Profile"""
    return ensure_master_profile(), None

def check_dependencies():
    return setup_library()

# =========================================================================================
# THREAD-SAFE SERVER UTILS
# =========================================================================================

class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

class FileServerContext:
    """Context manager for a temporary HTTP server serving a specific directory."""
    def __init__(self, root_dir):
        self.root_dir = root_dir
        self.port = 0
        self.httpd = None
        self.thread = None
        
    def __enter__(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('localhost', 0))
        self.port = sock.getsockname()[1]
        sock.close()
        
        def run():
            try:
                # We must change CWD for SimpleHTTPRequestHandler to serve correct files
                # BUT changing CWD is not thread-safe!
                # Better: Use partial to configure directory? 
                # SimpleHTTPRequestHandler serves os.getcwd().
                # Python 3.7+ allows binding `directory` argument in SimpleHTTPRequestHandler
                
                handler = lambda *args: QuietHandler(*args, directory=self.root_dir)
                self.httpd = socketserver.TCPServer(("localhost", self.port), handler)
                self.httpd.serve_forever()
            except Exception as e:
                print(f"Server Thread Error: {e}")
                
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        time.sleep(0.5) # Warmup
        return self.port

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            
def create_driver_helper(options):
    """
    Creates a Chrome driver with fallback logic for version mismatches.
    Includes RETRY logic for stability.
    """
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # 1. Try Default (Bundled preferred)
            try:
                path = get_chromedriver_path(ignore_bundled=False)
                service = Service(path)
                return webdriver.Chrome(service=service, options=options)
            except SessionNotCreatedException as e:
                # If it's a version mismatch, we might want to try fallback immediately
                # But if it's "Chrome instance exited", we want to RETRY.
                msg = str(e).lower()
                if "exited" in msg or "disconnected" in msg:
                    raise e # Re-raise to trigger retry loop
                
                print(f"Warning: Bundled/Default driver mismatch ({e}).")
                
                # OFF-LINE CHECK: If we are offline, we can't download.
                # We can't easily check for internet, but we can catch the WDM error.
                print("Attempting to download matching driver (Online Check)...")
                
                try:
                    # 2. Fallback: Ignore bundled, force WDM to resolve matching version
                    path = get_chromedriver_path(ignore_bundled=True)
                    service = Service(path)
                    return webdriver.Chrome(service=service, options=options)
                except Exception as e2:
                    print(f"Fallback failed (likely offline): {e2}")
                    print("\nCRITICAL ERROR: Chrome version mismatch in offline mode.")
                    print("SOLUTION: To run offline, you MUST bundle 'Chrome for Testing' that matches your driver.")
                    print(f"1. Download Chrome for Testing v144 (or matching your driver).")
                    print(f"2. Extract to: {os.path.join(BASE_DIR, 'bin', 'chrome-win64', 'chrome.exe')}")
                    print("System will then use the bundled Chrome instead of the system Chrome.\n")
                    raise e
                    
        except Exception as retry_e:
            print(f"Driver Creation Failed (Attempt {attempt+1}/{max_retries}): {retry_e}")
            if attempt < max_retries - 1:
                time.sleep(3) # Wait before retry
            else:
                raise retry_e # Give up after 3 tries

def setup_driver(download_dir=None, unique_profile=None):
    chrome_options = Options()
    
    # Use UNIQUE profile for parallel execution
    if unique_profile:
        profile_dir = unique_profile
    else:
        profile_dir = CHROME_DATA_DIR
        
    chrome_options.add_argument(f"--user-data-dir={profile_dir}")
    # chrome_options.add_argument("--headless=new") # Caused hang/timeout
    chrome_options.add_argument("--window-position=100,100") # DEBUG: visible window (was -2400,-2400)
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--no-default-browser-check")
    chrome_options.add_argument("--disable-component-update")
    
    # --- STABILITY FLAGS ---
    chrome_options.add_argument("--no-sandbox")
    # chrome_options.add_argument("--disable-gpu")  # DEBUG: disabled - ScreenAI may need GPU
    chrome_options.add_argument("--disable-dev-shm-usage")
    # -----------------------

    chrome_options.add_argument("--force-renderer-accessibility")
    chrome_options.add_argument("--enable-features=PdfOcr,ScreenAI") 
    
    # REQUIRED FOR "Exact Save as PDF" behavior
    chrome_options.add_argument("--kiosk-printing") 
    
    prefs = {
        "accessibility": {
            "pdf_ocr_always_active": True,
            "image_labels_enabled": True
        },
        "plugins.always_open_pdf_externally": False,
        "download.default_directory": download_dir, 
        "savefile.default_directory": download_dir,
        "printing.print_preview_sticky_settings.appState": json.dumps({
            "recentDestinations": [{"id": "Save as PDF", "origin": "local", "account": ""}],
            "selectedDestinationId": "Save as PDF",
            "version": 2
        })
    }
    chrome_options.add_experimental_option("prefs", prefs)
    chrome_options.add_argument("--window-size=1280,1024")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option('useAutomationExtension', False)
    
    # Check for bundled Chrome binary - PRIORITY for Portable/Offline
    bundled_chrome = os.path.join(BASE_DIR, "bin", "chrome-win64", "chrome.exe")
    if os.path.exists(bundled_chrome):
        # logging.info(f"Using bundled Chrome: {bundled_chrome}")
        chrome_options.binary_location = bundled_chrome
    
    # Use helper with fallback
    driver = create_driver_helper(chrome_options)
    return driver

def process_pdf(input_path, output_path, num_pages=None, update_callback=None, wait_per_page=1.0, comparison_interval=1.0):
    driver = None
    
    # Create valid temp profile for this thread/process
    import uuid
    thread_id = str(uuid.uuid4())[:8]
    unique_profile_dir = os.path.join(TEMP_DIR, f"ocr_chrome_data_{thread_id}")
    
    try:
        # Optimization: Copy ONLY 'ScreenAI' folder from the master CHROME_DATA_DIR to the new profile
        # ... logic for symlinking ...
        setup_library() # Ensures DEST_MASTER_DIR is populated
        
        if not os.path.exists(unique_profile_dir):
            if os.path.exists(DEST_MASTER_DIR):
                try:
                    shutil.copytree(DEST_MASTER_DIR, unique_profile_dir)
                except Exception as e:
                    print(f"Profile Clone Error: {e}")
                    os.makedirs(unique_profile_dir, exist_ok=True) # Fallback to empty
            else:
                os.makedirs(unique_profile_dir)

        # Clean session data to prevent Chrome from restoring old tabs
        default_dir = os.path.join(unique_profile_dir, "Default")
        if os.path.exists(default_dir):
            for session_file in ["Current Session", "Current Tabs", "Last Session", "Last Tabs"]:
                p = os.path.join(default_dir, session_file)
                if os.path.exists(p):
                    try: os.remove(p)
                    except: pass
            sessions_dir = os.path.join(default_dir, "Sessions")
            if os.path.exists(sessions_dir):
                try: shutil.rmtree(sessions_dir)
                except: pass

        # Now launch
        input_dir = os.path.dirname(os.path.abspath(input_path))
        file_name = os.path.basename(input_path)
        
        # Use FileServerContext -> then TempDir -> then Driver
        # IMPORTANT: Driver must be closed BEFORE FileServerContext shuts down!
        
        with FileServerContext(input_dir) as port:
            with tempfile.TemporaryDirectory() as temp_print_dir:
                try:
                    driver = setup_driver(download_dir=temp_print_dir, unique_profile=unique_profile_dir)
                    # driver.set_window_position(-2400, -2400)  # DEBUG: keep visible 

                    file_url = f"http://localhost:{port}/{urllib.parse.quote(file_name)}#page=5000"
                    driver.get(file_url)
                    
                    if update_callback: update_callback("OCR Running...", "info")
                    
                    n_pages = num_pages if num_pages else 1
                    initial_wait = n_pages * float(wait_per_page)
                    if update_callback: update_callback(f"Waiting {initial_wait:.1f}s for initial processing ({n_pages} pages)...", "debug")
                    time.sleep(initial_wait)
                    
                    def save_temp_pdf(name_suffix):
                        driver.execute_script("window.print();")
                        w_t0 = time.time()
                        while time.time() - w_t0 < 30:
                            pdfs = glob.glob(os.path.join(temp_print_dir, "*.pdf"))
                            candidate = None
                            for p in pdfs:
                                fname = os.path.basename(p)
                                if not fname.startswith("temp"):
                                    try:
                                        if os.path.getsize(p) > 0:
                                            candidate = p
                                            break
                                    except: pass
                            if candidate: return candidate
                            time.sleep(1)
                        return None

                    def get_pdf_text_len(p):
                        try:
                            reader = pypdf.PdfReader(p)
                            text = ""
                            for page in reader.pages: text += page.extract_text()
                            return len(text)
                        except: return 0

                    f_base_raw = save_temp_pdf("base")
                    if not f_base_raw: return False, "Print failed (Initial Snapshot)"
                    
                    current_base_path = os.path.join(temp_print_dir, "temp_0.pdf")
                    shutil.move(f_base_raw, current_base_path)
                    len_base = get_pdf_text_len(current_base_path)
                    if update_callback: update_callback(f"Initial Text Length: {len_base} chars", "debug")
                    
                    max_retries = 20
                    loop_count = 0
                    
                    while loop_count < max_retries:
                        loop_count += 1
                        if update_callback: update_callback(f"Loop {loop_count}: Waiting {comparison_interval}s...", "debug")
                        time.sleep(float(comparison_interval))
                        
                        f_new_raw = save_temp_pdf("new")
                        if not f_new_raw: return False, f"Print failed at check {loop_count}"
                        
                        current_new_path = os.path.join(temp_print_dir, f"temp_{loop_count}.pdf")
                        shutil.move(f_new_raw, current_new_path)
                        
                        len_new = get_pdf_text_len(current_new_path)
                        
                        if len_new > len_base:
                            msg = f"OCR Progress: {len_base} -> {len_new} chars (+{len_new - len_base})"
                            if update_callback: update_callback(msg, "debug")
                            try: os.remove(current_base_path)
                            except: pass
                            current_base_path = current_new_path
                            len_base = len_new
                        elif len_new == 0 and len_base == 0:
                            # OCR hasn't produced any text yet - keep waiting
                            if update_callback: update_callback(f"Loop {loop_count}: Still 0 chars, waiting for OCR to activate...", "debug")
                            try: os.remove(current_new_path)
                            except: pass
                        else:
                            msg = f"OCR Stable: {len_new} chars. Finishing..."
                            if update_callback: update_callback(msg, "info")

                            # Stable
                            if os.path.exists(output_path): os.remove(output_path)
                            shutil.move(current_new_path, output_path)
                            if current_base_path != current_new_path and os.path.exists(current_base_path):
                                 try: os.remove(current_base_path)
                                 except: pass
                            if update_callback: update_callback(f"✓ Completed: {output_path}", "success")
                            return True, None
                    
                    return False, "OCR Timeout - Text never stabilized"
                
                finally:
                    # QUIT DRIVER HERE - Inside Context Managers
                    if driver:
                        try:
                            pid = driver.service.process.pid
                            driver.quit()
                        except: pass
                        # Fallback Kill
                        try:
                            import subprocess
                            subprocess.call(['taskkill', '/F', '/T', '/PID', str(pid)], 
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        except: pass

    except Exception as e:
        return False, str(e)
    finally:
        # Clean unique profile (Driver is already dead)
        try: shutil.rmtree(unique_profile_dir, ignore_errors=True)
        except: pass
