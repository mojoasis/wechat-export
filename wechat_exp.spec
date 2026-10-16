# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH)
datas = [
    (str(ROOT / 'src/web/templates'), 'src/web/templates'),
    (str(ROOT / 'src/web/static'), 'src/web/static'),
    (str(ROOT / 'README.md'), '.'),
    (str(ROOT / 'docs/HTTP-API.md'), 'docs'),
    (str(ROOT / 'docs/DATABASE-SCHEMA.md'), 'docs'),
    (str(ROOT / 'screenshots'), 'screenshots'),
    (str(ROOT / 'tools/silk_decoder.exe'), 'tools'),
]
binaries = []
hiddenimports = [
    'Crypto.Cipher.AES', 'Crypto.Util.Padding', 'flask', 'werkzeug', 'jinja2',
    'markdown', 'blackboxprotobuf', 'zstandard', 'openpyxl', 'jieba',
    'jieba.posseg', 'requests', 'zhconv', 'lameenc', 'pypinyin', 'psutil',
]
for package in ('zhconv', 'jieba', 'keystone'):
    package_datas, package_binaries, package_imports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_imports

a = Analysis(
    [str(ROOT / 'src/main.py')],
    pathex=[str(ROOT / 'src')],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest', '_pytest', 'tkinter', '_tkinter', 'turtle', 'idlelib', 'ensurepip', 'pip', 'setuptools', 'wheel', 'pkg_resources', 'multiprocessing', 'concurrent.futures.process', 'lib2to3', 'xmlrpc', 'pydoc', 'doctest', 'bdb'],
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
    name='wechat_exp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,  # GNU strip can break Windows Python/OpenSSL DLLs.
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(ROOT / 'file_version_info.txt'),
)
