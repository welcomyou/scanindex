"""
Run Chrome for Testing once to initialize ScreenAI in the master profile.
After this, the master profile can be copied for OCR use.
"""
import os, sys, time, shutil

# Reuse paths from chrome_ocr_engine
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import tempfile
TEMP_DIR = tempfile.gettempdir()
MASTER_PROFILE = os.path.join(TEMP_DIR, "ocr_chrome_data")
SOURCE_TEMPLATE = os.path.join(BASE_DIR, "models", "ocr_chrome_userdata_ori")
BUNDLED_CHROME = os.path.join(BASE_DIR, "bin", "chrome-win64", "chrome.exe")

print(f"Master profile: {MASTER_PROFILE}")
print(f"Source template: {SOURCE_TEMPLATE}")
print(f"Chrome binary: {BUNDLED_CHROME}")

# Step 1: Copy source template to master (fresh start)
if os.path.exists(MASTER_PROFILE):
    print("Deleting old master profile...")
    shutil.rmtree(MASTER_PROFILE, ignore_errors=True)

print("Copying source template to master profile...")
shutil.copytree(SOURCE_TEMPLATE, MASTER_PROFILE)

# Clean session data so Chrome doesn't restore old tabs
default_dir = os.path.join(MASTER_PROFILE, "Default")
if os.path.exists(default_dir):
    for sf in ["Current Session", "Current Tabs", "Last Session", "Last Tabs"]:
        p = os.path.join(default_dir, sf)
        if os.path.exists(p):
            os.remove(p)
    sessions_dir = os.path.join(default_dir, "Sessions")
    if os.path.exists(sessions_dir):
        shutil.rmtree(sessions_dir)
    print("Session data cleaned.")

# Step 2: Launch Chrome with master profile + ScreenAI flags
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from chrome_ocr_engine import get_chromedriver_path

chrome_options = Options()
chrome_options.add_argument(f"--user-data-dir={MASTER_PROFILE}")
chrome_options.add_argument("--window-position=100,100")
chrome_options.add_argument("--window-size=1280,1024")
chrome_options.add_argument("--no-sandbox")
chrome_options.add_argument("--disable-dev-shm-usage")
chrome_options.add_argument("--force-renderer-accessibility")
chrome_options.add_argument("--enable-features=PdfOcr,ScreenAI")
chrome_options.add_argument("--no-first-run")
chrome_options.add_argument("--no-default-browser-check")

prefs = {
    "accessibility": {
        "pdf_ocr_always_active": True,
        "image_labels_enabled": True
    },
}
chrome_options.add_experimental_option("prefs", prefs)
chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])

if os.path.exists(BUNDLED_CHROME):
    chrome_options.binary_location = BUNDLED_CHROME
    print(f"Using bundled Chrome: {BUNDLED_CHROME}")

driver_path = get_chromedriver_path()
print(f"ChromeDriver: {driver_path}")

print("\nLaunching Chrome to initialize ScreenAI...")
print("Chrome will open — wait 30 seconds for ScreenAI to download/initialize...")

service = Service(driver_path)
driver = webdriver.Chrome(service=service, options=chrome_options)

# Navigate to a simple PDF to trigger ScreenAI activation
test_pdf = None
for f in os.listdir(BASE_DIR):
    if f.endswith('.pdf') and os.path.getsize(os.path.join(BASE_DIR, f)) < 2_000_000:
        test_pdf = os.path.join(BASE_DIR, f)
        break

if test_pdf:
    print(f"Loading test PDF: {test_pdf}")
    driver.get(f"file:///{test_pdf}")
else:
    print("No test PDF found, opening blank page")
    driver.get("about:blank")

# Wait for ScreenAI to initialize
print("\nWaiting 30 seconds for ScreenAI initialization...")
print("(Watch Chrome window — you should see the PDF loaded)")
for i in range(30, 0, -5):
    print(f"  {i}s remaining...")
    time.sleep(5)

# Check screen_ai folder
screen_ai = os.path.join(MASTER_PROFILE, "screen_ai")
print(f"\nscreen_ai folder exists: {os.path.exists(screen_ai)}")
if os.path.exists(screen_ai):
    for item in os.listdir(screen_ai):
        full = os.path.join(screen_ai, item)
        if os.path.isdir(full):
            file_count = sum(len(files) for _, _, files in os.walk(full))
            print(f"  Version {item}: {file_count} files")

# Close Chrome
print("\nClosing Chrome...")
try:
    driver.quit()
except:
    pass

# Now save this initialized profile back to source template
print("\n" + "="*60)
answer = input("Save this profile as new source template? (y/n): ")
if answer.lower() == 'y':
    print("Backing up old template...")
    backup = SOURCE_TEMPLATE + "_backup"
    if os.path.exists(backup):
        shutil.rmtree(backup)
    shutil.copytree(SOURCE_TEMPLATE, backup)

    print("Saving new template...")
    shutil.rmtree(SOURCE_TEMPLATE)
    shutil.copytree(MASTER_PROFILE, SOURCE_TEMPLATE)
    print(f"Done! New template saved to: {SOURCE_TEMPLATE}")
    print(f"Backup at: {backup}")
else:
    print("Skipped. Master profile ready at:", MASTER_PROFILE)

print("\nYou can now run ocr_app.py — OCR should work.")
