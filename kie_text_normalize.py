"""Compatibility wrapper for the old kie_text_normalize module."""

from scanindex.core.kie import text_normalize as _impl

globals().update(_impl.__dict__)

