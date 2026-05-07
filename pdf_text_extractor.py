"""Compatibility wrapper for the old pdf_text_extractor module."""

from scanindex.core.pdf import text_extractor as _impl

globals().update(_impl.__dict__)
