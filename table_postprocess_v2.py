"""Compatibility wrapper for the old table_postprocess_v2 module."""

from scanindex.core.tables import postprocess_v2 as _impl

globals().update(_impl.__dict__)
