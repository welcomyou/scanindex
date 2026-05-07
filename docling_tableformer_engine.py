"""Compatibility wrapper for the old docling_tableformer_engine module."""

from scanindex.core.tables import docling_tableformer_engine as _impl

globals().update(_impl.__dict__)
