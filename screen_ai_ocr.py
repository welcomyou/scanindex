"""Compatibility wrapper for the old screen_ai_ocr module."""

from scanindex.core.ocr import screen_ai as _impl

globals().update(_impl.__dict__)
