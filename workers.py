"""Compatibility wrapper for the old workers module."""

from scanindex.core.tables import export_worker as _impl

globals().update(_impl.__dict__)

