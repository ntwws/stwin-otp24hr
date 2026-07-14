# -*- mode: python ; coding: utf-8 -*-

import os
import sys


vc_runtime = []
for dll_name in ("vcruntime140.dll", "vcruntime140_1.dll"):
    dll_path = os.path.join(sys.base_prefix, dll_name)
    if os.path.exists(dll_path):
        vc_runtime.append((dll_path, "."))


a = Analysis(
    ['desktop_app.py'],
    pathex=[],
    binaries=vc_runtime,
    datas=[('1.ico', '.'), ('cloud_config.json', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='OTP24HR by STWIN',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['1.ico'],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='OTP24HR by STWIN',
)
