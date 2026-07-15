@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt
python -m PyInstaller --noconfirm --clean --onedir --windowed --name "OTP24HR by STWIN" --icon "1.ico" --add-data "1.ico;." --add-data "cloud_config.json;." --collect-all customtkinter desktop_app.py
if errorlevel 1 (
  echo.
  echo สร้าง EXE ไม่สำเร็จ
  pause
  exit /b 1
)
echo.
echo สร้างสำเร็จ: dist\OTP24HR by STWIN\OTP24HR by STWIN.exe
pause
