"""Compatibility wrapper for the old translations module."""

from scanindex.infra import translations as _impl

globals().update(_impl.__dict__)

