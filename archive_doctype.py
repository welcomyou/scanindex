"""Compatibility wrapper for the old archive_doctype module."""

from scanindex.core.digitization import doctype as _impl

globals().update(_impl.__dict__)
