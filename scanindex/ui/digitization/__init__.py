"""Digitization workflow user interface."""

from .container import ArchiveContainer
from .split_step import ArchiveStep1Split
from .extraction_step import ArchiveStep2Kie
from .signing_step import ArchiveStep3Sign

DigitizationView = ArchiveContainer
SplitStep = ArchiveStep1Split
ExtractionStep = ArchiveStep2Kie
SigningStep = ArchiveStep3Sign

__all__ = [
    "ArchiveContainer",
    "ArchiveStep1Split",
    "ArchiveStep2Kie",
    "ArchiveStep3Sign",
    "DigitizationView",
    "SplitStep",
    "ExtractionStep",
    "SigningStep",
]

