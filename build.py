# -*- coding: utf-8 -*-
"""PyInstaller 单文件构建脚本。"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
PY = sys.executable


def main() -> int:
    cmd = [
        PY, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile", "--windowed",
        "--name", "FPH仪表设置工具",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT),
        "--hidden-import", "pyvisa_py",
        "--hidden-import", "pyvisa_py.tcpip",
        "--collect-submodules", "pyvisa_py",
        str(ROOT / "main.py"),
    ]
    print(" ".join(cmd))
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
