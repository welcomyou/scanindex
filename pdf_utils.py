"""Compatibility wrapper for the old pdf_utils module."""

from scanindex.core.pdf import utils as _impl

globals().update(_impl.__dict__)
