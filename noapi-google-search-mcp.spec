# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import copy_metadata

datas = []
binaries = []
hiddenimports = []
datas += copy_metadata('noapi-google-search-mcp')
datas += copy_metadata('mcp')
datas += copy_metadata('standard-mcp')
tmp_ret = collect_all('playwright')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('cv2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# The HTTP transports import uvicorn/starlette lazily (see http_app.serve), and
# uvicorn resolves its loop/protocol implementations by name at runtime, so a
# static analysis pass misses them entirely.
for _pkg in ('uvicorn', 'starlette'):
    tmp_ret = collect_all(_pkg)
    datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# mcp.server.fastmcp builds its transports from these at call time.
hiddenimports += [
    'mcp.server.fastmcp',
    'mcp.server.sse',
    'mcp.server.streamable_http',
    'mcp.server.streamable_http_manager',
    'mcp.server.transport_security',
]


a = Analysis(
    # server.py defines no `__main__` guard, so pointing the build at it produced
    # a binary that imported the server and exited. scripts/frozen_entry.py calls
    # the real CLI.
    ['scripts/frozen_entry.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    a.binaries,
    a.datas,
    [],
    name='noapi-google-search-mcp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
