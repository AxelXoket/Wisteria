@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM ===========================================================================
REM  Wisteria voice (Chatterbox) - one-time install for THIS machine.
REM
REM  Installs into a DEDICATED venv (tts_env), NOT the app venv. The voice worker
REM  (backend\tts_worker.py) runs as a subprocess out of tts_env so Chatterbox's
REM  torch/transformers/numpy can never clash with the memory stack (fastembed/
REM  onnxruntime) in the app venv, and so the voice's ~5-6 GB can be freed by just
REM  killing the worker when voice is off.
REM
REM  CRITICAL: RTX 5080 is Blackwell (sm_120) -> needs torch built for CUDA 12.8
REM  (cu128). Chatterbox pulls an older torch; we OVERRIDE it to 2.8.0+cu128 last.
REM
REM  First run downloads are large (~2.5 GB torch + ~1 GB Chatterbox weights).
REM ===========================================================================

echo.
echo [1/4] Creating dedicated voice venv (tts_env, Python 3.12)...
uv venv tts_env --python 3.12
if errorlevel 1 goto :err

echo.
echo [2/4] Chatterbox + cleanup/audio deps...
uv pip install --python tts_env\Scripts\python.exe "chatterbox-tts>=0.1.7" "noisereduce>=3.0" "librosa>=0.10" "scipy>=1.11" "soundfile>=0.12" "sounddevice>=0.5" "audiotsm>=0.1.2"
if errorlevel 1 goto :err

echo.
echo [3/4] Pin PyTorch 2.8.0 + torchaudio to CUDA 12.8 (sm_120 for RTX 5080)...
REM  This REPLACES whatever torch Chatterbox pulled (usually 2.6.0 cpu/cu121).
uv pip install --python tts_env\Scripts\python.exe torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
if errorlevel 1 goto :err

echo.
echo [4/4] Verifying the voice venv sees the GPU (expect: 2.8.0+cu128 12.8 (12, 0))...
tts_env\Scripts\python.exe -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.get_device_capability())"

echo.
echo ============================================================================
echo  Done. Drop a reference clip at voices\wisteria.wav for a cloned voice.
echo  Launch the app (run-app.bat) and toggle the speaker button on.
echo  To move the voice off the GPU (free VRAM), set tts_device = "cpu" in
echo  backend\config.py (slower, but no VRAM contention with the LLM).
echo ============================================================================
goto :eof

:err
echo.
echo KURULUM HATASI. Ciktiyi kontrol et. En sik sebep: yanlis torch wheel'i
echo (cu128 olmali) ya da internet. Tekrar denemek icin bu bat'i yeniden calistir.
pause
