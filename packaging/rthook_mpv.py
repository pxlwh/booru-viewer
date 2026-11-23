import ctypes.util
from pathlib import Path
import sys

# python-mpv otherwise searches the host linker cache (or needs build tools).
if getattr(sys, 'frozen', False):
    bundled = str(Path(sys._MEIPASS) / 'mpv-lib' / 'libmpv.so.2')
    original = ctypes.util.find_library
    ctypes.util.find_library = lambda name: bundled if name == 'mpv' else original(name)
    try:
        import mpv
    finally:
        ctypes.util.find_library = original
