"""Compatibility wrapper for the old direct_ocr_engine module."""

from scanindex.core.ocr import direct_engine as _impl

globals().update(_impl.__dict__)
