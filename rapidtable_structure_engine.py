"""Compatibility wrapper for the old rapidtable_structure_engine module."""

from scanindex.core.tables import rapidtable_structure_engine as _impl

globals().update(_impl.__dict__)
