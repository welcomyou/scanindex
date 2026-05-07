"""Compatibility wrapper for the old file_utils module."""

from scanindex.infra import file_utils as _impl

globals().update(_impl.__dict__)

