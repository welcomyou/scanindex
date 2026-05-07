"""Compatibility wrapper for the old ocr_text_normalizer module."""

from scanindex.core.ocr import text_normalizer as _impl

globals().update(_impl.__dict__)
