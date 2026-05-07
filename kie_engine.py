"""Compatibility wrapper for the old kie_engine module."""

from scanindex.core.kie import engine as _impl

globals().update(_impl.__dict__)

