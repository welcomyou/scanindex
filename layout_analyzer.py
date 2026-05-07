"""Compatibility wrapper for the old layout_analyzer module."""

from scanindex.core.tables import layout_analyzer as _impl

globals().update(_impl.__dict__)
