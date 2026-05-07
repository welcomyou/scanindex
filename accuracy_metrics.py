"""Compatibility wrapper for the old accuracy_metrics module."""

from scanindex.core.ocr import accuracy_metrics as _impl

globals().update(_impl.__dict__)

