"""Compatibility wrapper for the old archive_session module."""

from scanindex.core.digitization import session as _impl

globals().update(_impl.__dict__)
