"""Compatibility wrapper for the old portable_utils module."""

from scanindex.infra import paths as _impl

globals().update(_impl.__dict__)

