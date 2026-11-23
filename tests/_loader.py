"""Load features.* submodules without running features/__init__.py
(which imports Flask, Telethon, yt-dlp helpers ...). Lets the detector /
pipeline tests run anywhere with just `requests` installed."""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if "features" not in sys.modules:
    pkg = types.ModuleType("features")
    pkg.__path__ = [os.path.join(ROOT, "features")]
    sys.modules["features"] = pkg
