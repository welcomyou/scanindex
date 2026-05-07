"""
PDF Utilities for OCR text correction.
APPROACH: Direct content stream byte replacement.
This preserves EXACT structure since we only change specific bytes.
"""

import fitz  # PyMuPDF
import pikepdf
import os
import re
import json
import sys
from typing import Optional


import difflib
from collections import defaultdict

from scanindex.core.kie.json_utils import write_corrected_companion_json

def find_word_replacements(original_text: str, corrected_text: str) -> dict[str, str]:
    """
    Compare original and corrected text using SequenceMatcher.
    - If a word/phrase CONSISTENTLY changes globally, add replacement.
    - If a word/phrase matches inconsistent targets, generate CONTEXTUAL replacements (3-gram).
    - Supports N-to-M replacements (e.g. 'foo,' -> 'foo ,') by treating segments as phrases.
    """
    orig_words = original_text.split()
    corr_words = corrected_text.split()
    
    s = difflib.SequenceMatcher(None, orig_words, corr_words)
    opcodes = s.get_opcodes()
    
    # 1. First Pass: Analyze Ambiguity of "Source Units"
    # A "Source Unit" can be a single word (1-1 map) or a phrase (N-M map)
    # But ambiguity tracking is tricky for phrases overlapping.
    # SIMPLIFICATION: 
    # - If 1-to-1: Track word ambiguity as before.
    # - If N-to-M: Track ambiguity of the joined original phrase.
    
    unit_variation_map = defaultdict(set)
    
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == 'equal':
            for k in range(i1, i2):
                w = orig_words[k]
                unit_variation_map[w].add(w)
        elif tag == 'replace':
            old_seg = orig_words[i1:i2]
            new_seg = corr_words[j1:j2]
            
            if len(old_seg) == len(new_seg):
                # 1-to-1: Treat each word individually
                for o, n in zip(old_seg, new_seg):
                    if len(o) > 1:
                        unit_variation_map[o].add(n)
            else:
                # N-to-M: Treat as whole block
                old_phrase = " ".join(old_seg)
                new_phrase = " ".join(new_seg)
                if len(old_phrase) > 1:
                    unit_variation_map[old_phrase].add(new_phrase)

    # Define ambiguous units (Explicit conflict or Substring collision)
    ambiguous_units = set()
    
    # 1. Explicit Conflict (Map to multiple targets)
    for u, targets in unit_variation_map.items():
        if len(targets) > 1:
            ambiguous_units.add(u)
            
    # 2. Substring Collision (Replacement candidate is substring of another token)
    # If we replace 'công' globally, we might corrupt 'công.' or 'thành công' if tokenized weirdly.
    # Optimization: Only check if a 'replacement candidate' is inside a 'protected token'.
    
    # Identify candidates for global replacement (currently not ambiguous)
    # and all tokens present in the text (vocabulary)
    
    # Pre-filtering keys to speed up
    all_tokens = list(unit_variation_map.keys())
    replacement_candidates = []
    
    for u in all_tokens:
        if u in ambiguous_units: continue
        
        # Check if this unit maps to something DIFFERENT than itself
        # If unit_variation_map[u] == {u}, it's purely kept.
        # If it maps to {v} where v != u, it is a replacement candidate.
        targets = unit_variation_map[u]
        if len(targets) == 1:
             target = list(targets)[0]
             if target != u:
                 replacement_candidates.append(u)

    # Check collisions
    for cand in replacement_candidates:
        # Check against all tokens (keepers and other replacements)
        for token in all_tokens:
            if cand == token: continue
            
            # If candidate is a substring of token
            if cand in token:
                 # Mark ambiguous to force contextual replacement
                 ambiguous_units.add(cand)
                 break

    replacements = {}
    
    # 2. Second Pass: Generate Replacements
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == 'replace':
            old_seg = orig_words[i1:i2]
            new_seg = corr_words[j1:j2]
            
            if len(old_seg) == len(new_seg):
                # 1-to-1
                for k, (o, n) in enumerate(zip(old_seg, new_seg)):
                    if len(o) < 2: continue
                    if o == n: continue
                    
                    if o not in ambiguous_units:
                        # Global Word Replacement
                        replacements[o] = n
                    else:
                        # Contextual Replacement
                        idx = i1 + k
                        prev_w = orig_words[idx - 1] if idx > 0 else ""
                        next_w = orig_words[idx + 1] if idx < len(orig_words) - 1 else ""
                        
                        # Build context phrase using original neighbors
                        parts_o = ([prev_w] if prev_w else []) + [o] + ([next_w] if next_w else [])
                        parts_n = ([prev_w] if prev_w else []) + [n] + ([next_w] if next_w else [])
                        
                        old_p = " ".join(parts_o)
                        new_p = " ".join(parts_n)
                        replacements[old_p] = new_p
            else:
                # N-to-M
                old_phrase = " ".join(old_seg)
                new_phrase = " ".join(new_seg)
                
                if len(old_phrase) < 2: continue
                if old_phrase == new_phrase: continue
                
                if old_phrase not in ambiguous_units:
                    # Global Phrase Replacement
                    replacements[old_phrase] = new_phrase
                else:
                    # Contextual Replacement for Phrase
                    # (Uses immediate neighbors of the block)
                    prev_w = orig_words[i1 - 1] if i1 > 0 else ""
                    next_w = orig_words[i2] if i2 < len(orig_words) else "" # i2 is exclusive end
                    
                    parts_o = ([prev_w] if prev_w else []) + [old_phrase] + ([next_w] if next_w else [])
                    parts_n = ([prev_w] if prev_w else []) + [new_phrase] + ([next_w] if next_w else [])
                    
                    old_p = " ".join(parts_o)
                    new_p = " ".join(parts_n)
                    replacements[old_p] = new_p
                    
    return replacements


def encode_pdf_string(text: str) -> bytes:
    """Encode text for PDF content stream."""
    # PDF strings can be in various encodings
    # Try UTF-16BE for Unicode support
    try:
        return text.encode('utf-16-be')
    except:
        return text.encode('latin-1', errors='replace')


def replace_in_content_stream(content_bytes: bytes, replacements: dict[str, str]) -> tuple[bytes, int]:
    """
    Replace text in PDF content stream bytes.
    Returns modified bytes and count of replacements.
    """
    count = 0
    result = content_bytes
    
    for old_text, new_text in replacements.items():
        # Try different encodings that PDF might use
        
        # 1. Try UTF-8 (common in modern PDFs)
        old_utf8 = old_text.encode('utf-8')
        new_utf8 = new_text.encode('utf-8')
        if old_utf8 in result:
            count += result.count(old_utf8)
            result = result.replace(old_utf8, new_utf8)
        
        # 2. Try Latin-1
        try:
            old_latin = old_text.encode('latin-1')
            new_latin = new_text.encode('latin-1')
            if old_latin in result:
                result = result.replace(old_latin, new_latin)
                count += 1
        except:
            pass
        
        # 3. Try to find as PDF hex string <...>
        old_hex = old_text.encode('utf-16-be').hex().upper()
        new_hex = new_text.encode('utf-16-be').hex().upper()
        old_hex_bytes = old_hex.encode('ascii')
        new_hex_bytes = new_hex.encode('ascii')
        if old_hex_bytes in result:
            result = result.replace(old_hex_bytes, new_hex_bytes)
            count += 1
    
    return result, count


def _copy_pdf_if_needed(src: str, dst: str):
    if os.path.abspath(src) == os.path.abspath(dst):
        return
    import shutil
    shutil.copy2(src, dst)


def _sync_companion_json(original_pdf_path: str, output_path: str,
                         replacements: dict[str, str], log,
                         correction_engine="proton_ct2_opt",
                         correction_mode="v8_final"):
    source_json = original_pdf_path + ".json"
    target_json = output_path + ".json"
    if not os.path.exists(source_json):
        return
    try:
        write_corrected_companion_json(
            source_json_path=source_json,
            target_json_path=target_json,
            replacements=replacements,
            output_pdf_path=output_path,
            correction_engine=correction_engine,
            correction_mode=correction_mode,
        )
        log(f"Saved companion JSON: {target_json}", "debug")
    except Exception as e:
        log(f"Warning: failed to sync companion JSON: {e}", "err")


def create_corrected_pdf(original_pdf_path: str, output_path: str, 
                          original_text: str, corrected_text: str,
                          log_callback=None) -> tuple[bool, str]:
    """
    Create corrected PDF by modifying content streams directly.
    This preserves exact structure.
    """
    def log(msg, level="info"): # Default to info
        if log_callback:
            # Check if callback accepts 2 args
            try:
                log_callback(msg, level)
            except:
                log_callback(msg)
        else:
            try:
                print(msg)
            except UnicodeEncodeError:
                # Do not let console encoding break the correction pipeline.
                safe_msg = str(msg) + os.linesep
                encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
                if hasattr(sys.stdout, "buffer"):
                    sys.stdout.buffer.write(safe_msg.encode(encoding, errors="replace"))
                    sys.stdout.flush()
                else:
                    print(safe_msg.encode("utf-8", errors="replace").decode("utf-8"))
    
    try:
        replacements = find_word_replacements(original_text, corrected_text)
        
        if not replacements:
            log("No differences found. Keeping original PDF and syncing JSON.", "debug")
            _copy_pdf_if_needed(original_pdf_path, output_path)
            _sync_companion_json(original_pdf_path, output_path, {}, log)
            return True, "No changes detected, copied original."
        
        log(f"Found {len(replacements)} word correction(s):", "debug")
        for i, (old, new) in enumerate(list(replacements.items())[:10]):
            log(f"  {i+1}. '{old}' → '{new}'", "debug")
        
        # Use pikepdf for direct content stream access
        pdf = pikepdf.open(original_pdf_path, allow_overwriting_input=True)
        total_replacements = 0
        
        for page_num, page in enumerate(pdf.pages):
            log(f"Processing page {page_num + 1}...", "debug")
            
            if '/Contents' not in page:
                continue
            
            contents = page['/Contents']
            
            # Handle array of streams or single stream
            if isinstance(contents, pikepdf.Array):
                streams = list(contents)
            else:
                streams = [contents]
            
            for stream_ref in streams:
                try:
                    # Resolve the stream object
                    if hasattr(stream_ref, 'objgen'):
                        stream = pdf.get_object(stream_ref.objgen)
                    else:
                        stream = stream_ref
                    
                    if not hasattr(stream, 'read_bytes'):
                        continue
                    
                    # Read content bytes
                    content_bytes = stream.read_bytes()
                    
                    # Replace text
                    modified_bytes, count = replace_in_content_stream(content_bytes, replacements)
                    
                    if count > 0:
                        log(f"  Modified {count} occurrences in stream", "debug")
                        stream.write(modified_bytes)
                        total_replacements += count
                    
                except Exception as e:
                    log(f"  Warning: Could not process stream: {e}", "err")
        
        # If byte-level replacement found nothing, try rebuild approach
        # (needed for direct_ocr_engine PDFs which use CID font encoding)
        if total_replacements == 0:
            pdf.close()
            log("Byte-level replacement found 0 matches, trying rebuild...", "debug")
            return _try_rebuild_fallback(original_pdf_path, output_path, replacements, log)

        # SAVE STRATEGY (Windows Robustness)
        temp_out = output_path + ".tmp"
        pdf.save(temp_out)
        pdf.close()

        if os.path.exists(output_path):
            os.remove(output_path)
        os.rename(temp_out, output_path)

        _sync_companion_json(original_pdf_path, output_path, replacements, log)
        log(f"Saved: {output_path}", "debug")
        return True, f"Applied {total_replacements} replacements in content streams."

    except Exception as e:
        import traceback
        traceback.print_exc()
        return False, f"Error: {str(e)}"


def _try_rebuild_fallback(original_pdf_path: str, output_path: str,
                          replacements: dict, log) -> tuple[bool, str]:
    """
    Fallback: rebuild PDF using stored OCR positions from companion JSON.
    Used when byte-level content stream replacement fails (CID font encoding).
    """
    json_path = original_pdf_path + ".json"
    if not os.path.exists(json_path):
        # No companion JSON - just copy original (best effort)
        log("No OCR positions JSON found, copying original.", "debug")
        _copy_pdf_if_needed(original_pdf_path, output_path)
        return True, "No byte-level matches and no OCR JSON; copied original."

    try:
        from scanindex.core.ocr.direct_engine import rebuild_pdf_with_text

        with open(json_path, "r", encoding="utf-8") as f:
            ocr_data = json.load(f)

        input_pdf = ocr_data.get("input_path", "")
        if not os.path.exists(input_pdf):
            # Fallback: use the _ocr.pdf itself as source (has original pages embedded)
            input_pdf = original_pdf_path

        success, msg = rebuild_pdf_with_text(
            input_pdf, output_path, json_path,
            replacements=replacements, log_callback=log
        )

        if success:
            _sync_companion_json(original_pdf_path, output_path, replacements, log)

        return success, msg

    except Exception as e:
        log(f"Rebuild fallback failed: {e}", "err")
        _copy_pdf_if_needed(original_pdf_path, output_path)
        return True, f"Rebuild failed ({e}), copied original."



def replace_words_in_pdf(pdf_path: str, output_path: str, 
                         replacements: dict[str, str],
                         log_callback=None) -> tuple[bool, str]:
    """Replace specific words in PDF."""
    original_text = " ".join(replacements.keys())
    corrected_text = " ".join(replacements.values())
    return create_corrected_pdf(pdf_path, output_path, original_text, corrected_text, log_callback)
