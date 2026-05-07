"""Compatibility wrapper for the old win_cert_store module."""

from scanindex.core.pdf import win_cert_store as _impl

globals().update(_impl.__dict__)

