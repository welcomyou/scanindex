"""Compatibility wrapper for the old screen_ai_downloader module."""

from scanindex.core.ocr import screen_ai_downloader as _impl

globals().update(_impl.__dict__)
