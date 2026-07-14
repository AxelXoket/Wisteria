@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM Gelistirme modunda uygulamayi calistirir (paketlemeden once test icin).
echo Wisteria baslatiliyor (ilk sefer model yuklemesi birkac saniye)...
uv run python main.py
