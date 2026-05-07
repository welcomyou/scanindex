"""Compatibility wrapper for the old kie_json_utils module."""

from scanindex.core.kie import json_utils as _impl

globals().update(_impl.__dict__)

