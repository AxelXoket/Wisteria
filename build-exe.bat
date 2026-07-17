@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM ===========================================================================
REM  Wisteria'u paketle - ARASTIRMA KARARLARI (2026-07):
REM
REM  * ONEDIR (onefile DEGIL): onefile her aciliste ~600MB'i %%TEMP%%'e acar
REM    (yavas ilk acilis) ve "kendini acan paket" deseni antivirus sezgilerinin
REM    tam hedefi (pyinstaller #6754). Onedir: hizli acilis + Defender-dostu.
REM  * PyInstaller (Nuitka degil): pywebview'in resmi yolu; pythonnet/clr ve
REM    onnxruntime icin hazir hook'lar var (pyinstaller-hooks-contrib).
REM  * --collect-all onnxruntime: hook provider DLL'lerini toplar ama DLL-yukleme
REM    hatalari icin topluluk cozumu collect-all (onnxruntime #25193).
REM  * --collect-all sqlite_vec: vec0.dll paket verisi (loadable_path ile yuklenir).
REM  * Surum kaynagi (version_info): imzasiz exe'de tutarli metadata AV/SmartScreen
REM    suphesini azaltir.
REM  * Windowed-subprocess kurali koda islendi: TUM cocuk surecler stdin dahil
REM    yonlendirilmis (yoksa frozen'da "handle is invalid" - resmi recete).
REM
REM  YERLESIM: cikti uygulama KOKUNE kopyalanir (Wisteria.exe + _internal\).
REM  Boylece app_dir() = bu klasor -> tts_env\, voices\, memory\, backend\
REM  tts_worker.py ve settings.json dev ile BIREBIR ayni cozulur; Models\ ve
REM  llama_cpp\ ust klasorden otomatik bulunur.
REM ===========================================================================

echo [0/4] JS sozdizimi kapisi (bozuk app.js paketlenemez)...
where node >nul 2>nul
if errorlevel 1 (
  echo !! node bulunamadi - sozdizimi kapisi ZORUNLUDUR. Bu kapinin atladigi
  echo !! hata sinifi sayfayi sessizce olduruyordu: UI sonsuza dek yuklemede kalir.
  echo !! node kur: winget install OpenJS.NodeJS.LTS - ya da PATH'e ekle.
  goto :err
)
node --check "web\app.js"
if errorlevel 1 (
  echo !! web\app.js SOZDIZIMI HATASI - once duzelt.
  goto :err
)

echo [1/4] Paketleme bagimliliklari (pyinstaller)...
uv sync --extra package
if errorlevel 1 goto :err

echo.
echo [2/4] PyInstaller onedir derlemesi (birkac dakika)...
uv run pyinstaller main.py ^
  --onedir --windowed --name Wisteria ^
  --contents-directory _internal ^
  --icon "assets\wisteria.ico" ^
  --version-file "assets\version_info.txt" ^
  --add-data "web;web" ^
  --add-data "assets\wisteria.ico;assets" ^
  --hidden-import clr ^
  --hidden-import apsw ^
  --collect-all webview ^
  --collect-all pythonnet ^
  --collect-all clr_loader ^
  --collect-all sqlite_vec ^
  --collect-all onnxruntime ^
  --collect-data fastembed ^
  --collect-data tokenizers ^
  --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module PySide6 ^
  --exclude-module tkinter --exclude-module test ^
  --noupx --clean --noconfirm
if errorlevel 1 goto :err

echo.
echo [3/4] Cikti app klasorune yerlestiriliyor (exe + _internal)...
if exist "_internal" rmdir /S /Q "_internal"
if exist "_internal" (
  echo !! _internal silinemedi - Wisteria.exe hala acik olabilir. Kapat ve tekrar dene.
  echo !! Eski _internal + yeni exe karisimi BOZUK paket uretir; devam edilmiyor.
  goto :err
)
xcopy /E /I /Y "dist\Wisteria\_internal" "_internal" >nul
if errorlevel 1 (
  echo !! _internal kopyalanamadi - yarim kopya bozuk paket demektir.
  goto :err
)
copy /Y "dist\Wisteria\Wisteria.exe" "Wisteria.exe" >nul
if errorlevel 1 goto :err

echo.
echo [4/4] Dogrulama...
if not exist "Wisteria.exe" ( echo !! Wisteria.exe yok & goto :err )
if not exist "_internal\web\index.html" ( echo !! web bundle eksik & goto :err )
if not exist "backend\tts_worker.py" ( echo !! tts_worker.py yok & goto :err )
if not exist "voices\wisteria.wav" ( echo UYARI: voices\wisteria.wav yok - ses klonu calismaz )
if not exist "tts_env\Scripts\python.exe" ( echo UYARI: tts_env yok - install-tts.bat calistir )

echo.
echo ============================================================================
echo  TAMAM. Calistir: Wisteria.exe  (bu klasorden; kisayol olusturabilirsin)
echo   - Sifreli hafiza/promptlar (memory\) ve ayarlar (settings.json) dev
echo     surumuyle ORTAK - ayni parola, ayni veriler.
echo   - dist\ klasoru ara ciktidir; silinebilir.
echo ============================================================================
goto :eof

:err
echo.
echo PAKETLEME HATASI - ciktiyi kontrol et.
pause
