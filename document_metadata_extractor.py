"""Compatibility wrapper for the old document_metadata_extractor module."""

from scanindex.core.digitization import metadata_extractor as _impl

globals().update(_impl.__dict__)
