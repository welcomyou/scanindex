"""Compatibility wrapper for the old archive_fuzzy module."""

from scanindex.core.digitization import fuzzy as _impl

globals().update(_impl.__dict__)
