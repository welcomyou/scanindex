"""Compatibility wrapper for the old accuracy_baseline module."""

from scanindex.core.ocr import accuracy_baseline as _impl

globals().update(_impl.__dict__)

