@echo off
rem Model Hub launcher.
rem   start.bat           = auto (download on if huggingface_hub is installed)
rem   start.bat offline   = force offline / air-gapped mode
cd /d "%~dp0"
if /i "%1"=="offline" (set MODEL_HUB_OFFLINE=1) else (set MODEL_HUB_OFFLINE=0)
if not defined PORT set PORT=8767
echo Model Hub: http://127.0.0.1:%PORT%  (offline=%MODEL_HUB_OFFLINE%)
python -m uvicorn main:app --host 127.0.0.1 --port %PORT%
