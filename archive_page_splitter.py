"""Compatibility wrapper for the old archive_page_splitter module."""

from scanindex.core.digitization import page_splitter as _impl

globals().update(_impl.__dict__)
