"""Tests run on Linux (CI + dev) and Windows. Windows-only APIs are faked; every
test gets its own HEADROOM_DATA dir so nothing touches a real install."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("HEADROOM_DATA", tempfile.mkdtemp(prefix="headroom-test-"))
os.environ.setdefault("LOCALAPPDATA", os.environ["HEADROOM_DATA"])
