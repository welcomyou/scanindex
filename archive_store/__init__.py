"""Compatibility package for legacy archive_store imports."""

from scanindex.core import repository as _repository

__path__ = _repository.__path__

