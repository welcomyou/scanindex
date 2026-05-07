@echo off
echo Cleaning up GPU libraries and enforcing CPU versions...

echo Uninstalling torch (GPU version possible)...
pip uninstall -y torch torchvision torchaudio

echo Uninstalling ctranslate2...
pip uninstall -y ctranslate2

echo Installing torch (CPU version)...
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu

echo Reinstalling ctranslate2...
pip install ctranslate2

echo Done!
pause
