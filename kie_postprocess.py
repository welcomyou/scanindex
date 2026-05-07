"""Compatibility wrapper for the old kie_postprocess module."""

from scanindex.core.kie import postprocess as _impl

globals().update(_impl.__dict__)

