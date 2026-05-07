from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldSpec:
    label: str
    multi_instance: bool
    page_preference: str
    use_line_spans: bool
    use_word_windows: bool
    max_lines: int
    max_words: int
    positive_f1: float
    positive_recall: float
    ignore_f1: float


FIELD_SPECS: dict[str, FieldSpec] = {
    "REGIME_HEADER": FieldSpec("REGIME_HEADER", False, "primary", True, False, 2, 0, 0.88, 0.85, 0.20),
    "ISSUE_ORG_SUPERIOR": FieldSpec("ISSUE_ORG_SUPERIOR", False, "primary", True, False, 4, 0, 0.88, 0.85, 0.20),
    "ISSUE_ORG_NAME": FieldSpec("ISSUE_ORG_NAME", False, "primary", True, False, 6, 0, 0.88, 0.85, 0.20),
    "DOC_NUMBER_SYMBOL": FieldSpec("DOC_NUMBER_SYMBOL", False, "primary", True, True, 3, 16, 0.92, 0.90, 0.20),
    "PLACE_DATE": FieldSpec("PLACE_DATE", False, "primary", True, True, 3, 16, 0.92, 0.90, 0.20),
    "DOC_SUBJECT": FieldSpec("DOC_SUBJECT", False, "primary", True, False, 8, 0, 0.88, 0.85, 0.20),
    "ADDRESSEE": FieldSpec("ADDRESSEE", False, "primary", True, False, 15, 0, 0.85, 0.80, 0.20),
    "RECIPIENTS": FieldSpec("RECIPIENTS", False, "signature", True, False, 20, 0, 0.85, 0.80, 0.20),
    "SIGNER_ROLE": FieldSpec("SIGNER_ROLE", True, "signature", True, False, 6, 0, 0.86, 0.82, 0.20),
    "SIGNER_NAME": FieldSpec("SIGNER_NAME", True, "signature", True, True, 3, 8, 0.92, 0.90, 0.20),
}


LABELS = list(FIELD_SPECS)
SINGLE_INSTANCE_FIELDS = [spec.label for spec in FIELD_SPECS.values() if not spec.multi_instance]
MULTI_INSTANCE_FIELDS = [spec.label for spec in FIELD_SPECS.values() if spec.multi_instance]
