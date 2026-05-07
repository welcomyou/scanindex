"""Compatibility wrapper for the old table_anchored_merger module."""

from scanindex.core.tables import docx_exporter as _impl

globals().update(_impl.__dict__)
