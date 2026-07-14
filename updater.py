from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
import urllib.parse
from pathlib import Path


def version_tuple(value: str) -> tuple[int, ...]:
    clean = str(value).strip().lower().lstrip("v")
    parts = clean.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        raise ValueError(f"เวอร์ชันไม่ถูกต้อง: {value}")
    return tuple(int(part) for part in parts)


class UpdateManager:
    def __init__(self, current_version: str, manifest_url: str):
        self.current_version = current_version
        self.manifest_url = manifest_url

    def check(self):
        request = urllib.request.Request(self.manifest_url, headers={"User-Agent": "OTP24HR-Updater/1.0", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(request, timeout=20) as response:
            manifest = json.loads(response.read().decode("utf-8"))
        for key in ("version", "download_url", "sha256"):
            if not manifest.get(key):
                raise ValueError(f"update.json ไม่มีค่า {key}")
        manifest["available"] = version_tuple(manifest["version"]) > version_tuple(self.current_version)
        return manifest

    def download_verified(self, manifest, progress=None):
        folder = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "OTP24HR" / "updates"
        folder.mkdir(parents=True, exist_ok=True)
        package_type = str(manifest.get("package_type", "")).lower()
        url_suffix = Path(urllib.parse.urlparse(manifest["download_url"]).path).suffix.lower()
        suffix = ".zip" if package_type == "zip" or url_suffix == ".zip" else ".exe"
        target = folder / f"OTP24HR-{manifest['version']}{suffix}"
        request = urllib.request.Request(manifest["download_url"], headers={"User-Agent": "OTP24HR-Updater/1.0"})
        digest = hashlib.sha256()
        with urllib.request.urlopen(request, timeout=60) as response, open(target, "wb") as stream:
            total = int(response.headers.get("Content-Length", "0")); received = 0
            while True:
                chunk = response.read(1024 * 256)
                if not chunk: break
                stream.write(chunk); digest.update(chunk); received += len(chunk)
                if progress: progress(received, total)
        actual = digest.hexdigest().lower(); expected = str(manifest["sha256"]).strip().lower()
        if actual != expected:
            try: target.unlink()
            except OSError: pass
            raise ValueError("SHA-256 ของไฟล์อัปเดตไม่ตรงกัน")
        return str(target)

    @staticmethod
    def install_and_restart(update_file: str):
        if not getattr(sys, "frozen", False):
            raise RuntimeError("ติดตั้งอัปเดตได้เฉพาะโปรแกรม EXE")
        current = os.path.abspath(sys.executable); update_file = os.path.abspath(update_file)
        if update_file.lower().endswith(".zip"):
            current_dir = os.path.dirname(current)
            stage = str(Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "OTP24HR" / "update-stage")
            script = Path(tempfile.gettempdir()) / "otp24hr-update.ps1"
            def ps_quote(value):
                return "'" + str(value).replace("'", "''") + "'"
            content = (
                "$ErrorActionPreference = 'Stop'\r\n"
                "Start-Sleep -Seconds 2\r\n"
                f"$package = {ps_quote(update_file)}\r\n"
                f"$stage = {ps_quote(stage)}\r\n"
                f"$target = {ps_quote(current_dir)}\r\n"
                f"$exe = {ps_quote(current)}\r\n"
                "Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue\r\n"
                "Expand-Archive -LiteralPath $package -DestinationPath $stage -Force\r\n"
                "Get-ChildItem -LiteralPath $stage -Force | Copy-Item -Destination $target -Recurse -Force\r\n"
                "Start-Process -FilePath $exe\r\n"
                "Remove-Item -LiteralPath $package -Force -ErrorAction SilentlyContinue\r\n"
                "Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue\r\n"
                "Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue\r\n"
            )
            # PowerShell 5.1 needs a BOM for non-ASCII paths, but the frozen
            # build may not include Python's optional utf_8_sig codec.
            script.write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                creationflags=0x08000000,
            )
            return
        new_exe = update_file
        script = Path(tempfile.gettempdir()) / "otp24hr-update.cmd"
        content = (
            "@echo off\r\n"
            "timeout /t 2 /nobreak >nul\r\n"
            f'copy /y "{new_exe}" "{current}" >nul\r\n'
            f'start "" "{current}"\r\n'
            f'del /q "{new_exe}" >nul 2>&1\r\n'
            'del /q "%~f0" >nul 2>&1\r\n'
        )
        script.write_text(content, encoding="utf-8")
        subprocess.Popen(["cmd.exe", "/c", str(script)], creationflags=0x08000000)
