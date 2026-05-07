"""Compatibility wrapper for the old chrome_ocr_engine module."""

from scanindex.core.ocr import chrome_engine as _impl

globals().update(_impl.__dict__)
