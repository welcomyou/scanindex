"""Compatibility wrapper for the old archive_pipeline module."""

from scanindex.core.digitization import runner as _impl

globals().update(_impl.__dict__)
