"""Compatibility wrapper for the old pdf_a_converter module."""

from scanindex.core.pdf import pdfa_converter as _impl

globals().update(_impl.__dict__)
