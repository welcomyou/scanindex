"""Compatibility wrapper for the old archive_output module."""

from scanindex.core.digitization import metadata_export as _impl

globals().update(_impl.__dict__)
