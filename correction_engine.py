"""Compatibility wrapper for the old correction_engine module."""

from scanindex.core.correction import engine as _impl

globals().update(_impl.__dict__)
