"""llama-server lifecycle: reuse an already-running server, else spawn our own.

Spawning uses a Windows Job Object with KILL_ON_JOB_CLOSE so the server is never
orphaned if the app closes or crashes. Only a server we started is torn down.
"""

from __future__ import annotations

import socket
import subprocess
import threading
import time
from typing import Callable

import httpx

from .config import CONFIG
from .logutil import err_brief, log_for

_log = log_for("server")

try:  # Windows job-object + process handles
    import win32api  # type: ignore
    import win32con  # type: ignore
    import win32job  # type: ignore
    _HAS_WIN32 = True
except Exception:  # pragma: no cover
    _HAS_WIN32 = False

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DEFAULT_PORT = 8080


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _healthy(base_url: str) -> bool:
    try:
        headers = {"Authorization": f"Bearer {CONFIG.api_key}"} if CONFIG.api_key else {}
        r = httpx.get(f"{base_url}/health", timeout=2.5, headers=headers)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


class ServerManager:
    def __init__(self) -> None:
        self.port: int | None = None
        self.proc: subprocess.Popen | None = None
        self._job = None
        self._owned = False
        # ensure/stop/shutdown serilestirilir; _closing kapanistan sonra yeni
        # spawn'i KALICI engeller (kapanisla yarisan ctx-restart, kapanis
        # sirasinda 7GB'lik taze model yuklemesi baslatabiliyordu - denetim O5).
        self._lock = threading.Lock()
        self._closing = False

    @property
    def base_url(self) -> str:
        return CONFIG.api_base(self.port or DEFAULT_PORT)

    @property
    def owned(self) -> bool:
        """Sunucuyu biz mi calistiriyoruz (False = disaridaki sunucu reuse)."""
        return self._owned

    def _spawn_args(self, port: int) -> list[str]:
        return [
            str(CONFIG.llama_server),
            "-m", str(CONFIG.model_path),
            "--mmproj", str(CONFIG.mmproj_path),
            "--alias", CONFIG.alias,
            "--host", CONFIG.host,
            "--port", str(port),
            "--api-key", CONFIG.api_key,
            "--offline", "--no-ui", "--no-slots", "--jinja",
            "-ngl", str(CONFIG.ngl), "-t", str(CONFIG.threads),
            "-c", str(CONFIG.n_ctx), "-n", str(CONFIG.n_predict),
            "-np", str(CONFIG.n_parallel),
            # NOT: --context-shift bilerek YOK - mmproj/vision yuklu oldugunda
            # llama.cpp shift'i zaten devre disi birakir; bayrak sadece kafa karistirir.
            "--flash-attn", "on",
            "--cache-type-k", CONFIG.cache_type_k,
            "--cache-type-v", CONFIG.cache_type_v,
            "--ubatch-size", str(CONFIG.ubatch),
            "--image-max-tokens", str(CONFIG.image_max_tokens),
            "--image-min-tokens", str(CONFIG.image_min_tokens),
            "--keep", "2048",
        ]

    def _make_job(self) -> None:
        if not _HAS_WIN32 or self.proc is None:
            return
        try:
            job = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
            info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
            h = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, self.proc.pid)
            win32job.AssignProcessToJobObject(job, h)
            self._job = job  # keep alive for the app's lifetime
        except Exception as e:
            # yetim korumasi devre disi kaldiginda artik SESSIZ degil: cokme
            # sonrasi VRAM tutan hayalet surecin tek izi bu satir olur
            _log.warning("job object atanamadi (yetim korumasi yok) err=%s", err_brief(e))
            self._job = None

    def ensure(self) -> tuple[bool, str]:
        """Reuse a running server or spawn one. Returns (spawned_new, message)."""
        with self._lock:
            if self._closing:
                return (False, "closing")
            return self._ensure_locked()

    def _ensure_locked(self) -> tuple[bool, str]:
        # 0) zaten bizim canli surecimiz varsa yenisini acma (cift-boot kemeri)
        if self._owned and self.proc is not None and self.proc.poll() is None:
            return (False, "already_running")

        # 1) reuse an already-running server on the default port
        if _healthy(CONFIG.api_base(DEFAULT_PORT)):
            self.port = DEFAULT_PORT
            self.proc = None   # bayat sahiplenilmis proc kalmasin: wait_ready'nin
            self._job = None   # poll fast-fail'i sagligi olu surece bakip yaniltiyordu
            self._owned = False
            return (False, "reused")

        # 2) validate the required files before spawning
        if not CONFIG.llama_server.exists():
            return (False, f"missing:llama-server ({CONFIG.llama_server})")
        if not CONFIG.model_path.exists():
            return (False, f"missing:model ({CONFIG.model_path})")

        # 3) spawn our own on a free port; port araligi calinirsa (TOCTOU)
        #    BIR kez taze portla yeniden dene
        last_err = "spawn_failed:early_exit"
        for attempt in (1, 2):
            self.port = _free_port()
            try:
                self.proc = subprocess.Popen(
                    self._spawn_args(self.port),
                    cwd=str(CONFIG.root),
                    creationflags=_CREATE_NO_WINDOW,
                    # ALL three stdio handles must be redirected: in a --windowed frozen
                    # app there is no console, and an unredirected stdin makes Popen die
                    # with "OSError: [WinError 6] handle is invalid" (PyInstaller recipe).
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception as exc:
                return (False, f"spawn_failed:{type(exc).__name__}")
            self._owned = True
            self._make_job()
            time.sleep(0.8)  # bind hatasi ilk yuz ms'de olur; erken olumu yakala
            if self.proc.poll() is None:
                return (True, "spawned")
            _log.warning("llama-server hemen sonlandi (port=%d deneme=%d)", self.port, attempt)
        return (False, last_err)

    def wait_ready(self, timeout: int = 180, on_progress: Callable[[int], None] | None = None) -> bool:
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            proc = self.proc  # es zamanli stop() None'a cevirebilir - yerel kopya
            if proc is not None and proc.poll() is not None:
                return False  # process died during load
            if _healthy(self.base_url):
                return True
            if on_progress:
                on_progress(int(time.monotonic() - start))
            time.sleep(1.2)
        return _healthy(self.base_url)

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def shutdown(self) -> None:
        """Kapanis yolu: durdur VE yeni spawn'lari kalici engelle."""
        with self._lock:
            self._closing = True
            self._stop_locked()

    def _stop_locked(self) -> None:
        proc = self.proc
        self.proc = None   # bayat referans hicbir karari yaniltmasin
        self._job = None
        if not self._owned:
            return
        self._owned = False
        if proc is None:
            return
        try:
            proc.terminate()
        except Exception:
            pass
        # Job object closing (on GC/exit) kills the tree; also try taskkill as a fallback.
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           creationflags=_CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        except Exception:
            pass
