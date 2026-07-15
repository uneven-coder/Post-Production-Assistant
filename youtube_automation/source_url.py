"""Grabs a YouTube video ID out of the clipboard so `--youtube-silent-only` can be
pointed at a video without any manual copy/paste step into the app itself."""
import ctypes
import re
from typing import Optional

_CF_UNICODETEXT = 13


def get_clipboard_text() -> str:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # GlobalLock/GetClipboardData return handles/pointers - ctypes defaults their
    # return type to a 32-bit int, which truncates real 64-bit addresses on Win64 and
    # hands wstring_at() garbage. Must be declared explicitly as c_void_p.
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]

    if not user32.OpenClipboard(0):
        return ""
    try:
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


# Studio URLs look like studio.youtube.com/video/{id}/edit, .../livestreaming, or just
# .../video/{id} with no trailing segment at all - the ID is always the fixed-width
# path segment right after "video/". youtu.be and watch?v= links are matched too since
# they're common enough to be worth handling even though the task at hand is Studio-only.
_ID = r"[A-Za-z0-9_-]{11}"
_URL_PATTERNS = [
    re.compile(rf"studio\.youtube\.com/video/({_ID})(?:/|\?|#|$)"),
    re.compile(rf"youtu\.be/({_ID})(?:[/?#]|$)"),
    re.compile(rf"[?&]v=({_ID})(?:&|#|$)"),
]


def extract_video_id(text: str) -> Optional[str]:
    if not text:
        return None
    for pattern in _URL_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(1)
    return None
