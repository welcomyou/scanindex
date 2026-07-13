"""Version-per-file migration for portable upgrade safety.

Problem this module solves
--------------------------
ScanIndex ships as a portable folder. Users upgrade by copying a newer
release folder *over* an older one with Windows Explorer. Because every
release used the same filenames (``settings.ini``, ``repository/repository.db``),
that copy silently overwrote the user's real settings and archive DB with
the new release's fresh/empty defaults — **data loss**.

Strategy: Expand-Contract (a.k.a. version-per-file)
---------------------------------------------------
Each release reads/writes its own *versioned* filenames, derived from the
app version::

    settings-<APP_VERSION>.ini            e.g. settings-1.1.4.ini
    repository/repository-<APP_VERSION>.db
    config/sign_settings-<APP_VERSION>.json
    ...

Two distinct version concepts, two roles:

* **App version** (``get_version_short()`` → "1.1.4") drives the *filename*.
  Every release gets a different name, so copying a new release folder over
  an old one can never overwrite the old data (different filenames).
* **Schema version** (``C.SCHEMA_VERSION`` → "8") drives the *data converter*.
  When the on-disk DB predates the current schema, ``schema_converters`` runs
  a chain of v_n → v_(n+1) converters. App-version renames and schema
  conversion are independent: a release may bump the app version without
  changing the schema (then rename only, no conversion) or vice-versa.

On startup the app calls :func:`run_startup_migration` (text config files)
and :func:`migrate_db_if_needed` (SQLite archive). Each merges any older
file into the current-version filename, then deletes the older file — so at
any moment only one versioned file exists per resource (no disk bloat).

A *fresh* install (no legacy file at all) writes nothing to disk; defaults
live in RAM until the user explicitly saves.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Callable, Optional

# NOTE: heavy/infra imports (get_base_dir, get_resource_path, get_version_short)
# are deferred into function bodies to avoid import cycles. This module is the
# earliest thing imported at startup; paths.py must not import us at module
# level, and version.py pulls in subprocess so we keep it lazy too.

_LOG_PREFIX = "[Versioning]"


def _log(msg: str) -> None:
    print(f"{_LOG_PREFIX} {msg}")


# --------------------------------------------------------------------- version

def _app_version() -> str:
    """Current app version, e.g. "1.1.4". Lazy import to avoid cycles."""
    from scanindex.infra.version import get_version_short
    return get_version_short()


def versioned_name(base: str, ext: str, version: Optional[str] = None) -> str:
    """``("settings", ".ini")`` → ``"settings-1.1.4.ini"``.

    ``base`` may itself contain a path (``"config/sign_settings"``); only the
    final stem gets the version suffix.
    """
    ver = version if version is not None else _app_version()
    head, tail = os.path.split(base)
    return f"{head}/{tail}-{ver}{ext}" if head else f"{tail}-{ver}{ext}"


_VERSION_TAIL_RE = re.compile(r"^(?P<base>.+?)-(?P<ver>\d+\.\d+(?:\.\d+)*)$")


def parse_app_version_from_name(path: Path) -> Optional[tuple[int, ...]]:
    """``Path("settings-1.1.4.ini")`` → ``(1, 1, 4)``.

    ``Path("settings.ini")`` → ``None`` (legacy, no version in name).
    """
    stem = path.stem  # "settings-1.1.4" (drops ".ini")
    m = _VERSION_TAIL_RE.match(stem)
    if not m:
        return None
    try:
        return tuple(int(x) for x in m.group("ver").split("."))
    except ValueError:
        return None


# ------------------------------------------------------------------ path helpers

def _base_dir() -> Path:
    from scanindex.infra.paths import get_base_dir
    return Path(get_base_dir())


def find_versioned_file(base_dir: Path, base: str, ext: str,
                        version: Optional[str] = None) -> Optional[Path]:
    """Return the path of *this* app version's file if it exists, else None."""
    rel = versioned_name(base, ext, version)
    p = base_dir / rel
    return p if p.exists() else None


def find_legacy_files(base_dir: Path, base: str, ext: str) -> list[Path]:
    """Return older-version files + the bare legacy name, newest first.

    "Legacy" = any candidate that is NOT the current app version. Includes:
      * the un-versioned bare name (``settings.ini``) — pre-versioning layout
        used by 1.1.3 and earlier.
      * files carrying an app version older than the running one
        (``settings-1.1.3.ini``).
    Sorted by parsed version descending; bare-name sorts last (oldest).
    """
    head, _tail = os.path.split(base)
    search_dir = base_dir / head if head else base_dir
    if not search_dir.exists():
        return []

    stem = os.path.split(base)[1]
    current = _app_version()
    out: list[Path] = []
    for entry in search_dir.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix != ext:
            continue
        if entry.stem == stem:
            # Bare legacy name (settings.ini).
            out.append(entry)
            continue
        m = _VERSION_TAIL_RE.match(entry.stem)
        if not m or m.group("base") != stem:
            continue
        ver_str = m.group("ver")
        if ver_str == current:
            continue  # own file, not legacy
        try:
            v = tuple(int(x) for x in ver_str.split("."))
        except ValueError:
            continue
        if _vcmp(v, _ver_tuple(current)) >= 0:
            continue  # same/newer — not a legacy to migrate from
        out.append((entry, v))

    def _key(item):
        # versioned files first by version desc; bare name last.
        if isinstance(item, tuple):
            return (0, item[1])
        return (1, (0,))

    out.sort(key=_key)
    return [it[0] if isinstance(it, tuple) else it for it in out]


def _ver_tuple(s: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in s.split("."))
    except ValueError:
        return (0,)


def _vcmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    # Zero-pad to equal length then compare.
    la, lb = len(a), len(b)
    if la < lb:
        a = a + (0,) * (lb - la)
    elif lb < la:
        b = b + (0,) * (la - lb)
    return (a > b) - (a < b)


# ------------------------------------------------------------ public resolvers

def get_active_settings_path() -> str:
    """Absolute path to this version's settings file. Used everywhere the
    codebase used to hardcode ``settings.ini``.

    Importing this is cheap; the lookup itself is a couple of ``os.path``
    calls. Safe to call at module import time of UI modules (theme.py,
    signing_step.py) AFTER :func:`run_startup_migration` has run.
    """
    from scanindex.infra.paths import get_resource_path, get_base_dir
    base_dir = Path(get_base_dir())
    own = find_versioned_file(base_dir, "settings", ".ini")
    if own is not None:
        return str(own)
    # Not present yet (fresh, or user hasn't saved). Return the *expected*
    # versioned path so a subsequent write lands on the right name. Resolve
    # via get_resource_path so the bundle fallback still works for samples.
    return get_resource_path(versioned_name("settings", ".ini"))


def get_active_db_filename() -> str:
    """Filename (not path) of this version's SQLite DB, e.g.
    ``"repository-1.1.4.db"``. The folder is the resolved repository dir."""
    return versioned_name("repository", ".db")


def get_active_config_path(base: str, ext: str) -> str:
    """Absolute path to this version's config file for an arbitrary resource,
    e.g. ``get_active_config_path("config/sign_settings", ".json")``.
    Mirrors :func:`get_active_settings_path` for the non-INI config files
    (sign_settings.json, sign_templates.json) read at module import time."""
    from scanindex.infra.paths import get_base_dir, get_resource_path
    base_dir = Path(get_base_dir())
    own = find_versioned_file(base_dir, base, ext)
    if own is not None:
        return str(own)
    return get_resource_path(versioned_name(base, ext))


# ------------------------------------------------------------ text-file migration

# (base, ext, example_relative_path). Example is read-only sample bundled in
# the release; used only to fall back when neither own nor legacy exists AND
# the caller explicitly wants to seed (we don't, for fresh installs — see
# run_startup_migration CASE b).
_TEXT_FILES: list[tuple[str, str, str]] = [
    ("settings", ".ini", "settings.ini.example"),
    ("config/sign_settings", ".json", "config/sign_settings.json.example"),
    ("config/sign_templates", ".json", "config/sign_templates.json.example"),
    ("ignored_words", ".txt", "ignored_words.txt.example"),
]

_AUTO_SEED_MARKER_SECTION = "Meta"
_AUTO_SEED_MARKER_KEY = "auto_seeded"


def _is_auto_seeded_ini(path: Path) -> bool:
    """True if a settings .ini was machine-generated from an example (carries
    the ``[Meta] auto_seeded`` marker). User-initiated saves clear it."""
    if not path.exists() or path.suffix != ".ini":
        return False
    try:
        import configparser
        cfg = configparser.ConfigParser()
        cfg.read(path, encoding="utf-8")
        return (cfg.has_section(_AUTO_SEED_MARKER_SECTION)
                and cfg.has_option(_AUTO_SEED_MARKER_SECTION, _AUTO_SEED_MARKER_KEY))
    except Exception:
        return False


def _is_empty_or_default(path: Path, example_path: Optional[Path]) -> bool:
    """Heuristic for non-INI files (json/txt): empty, or byte-identical to the
    bundled example. Used to decide whether the own file should yield to a
    legacy file with real data."""
    if not path.exists():
        return True
    try:
        if path.stat().st_size == 0:
            return True
    except OSError:
        return False
    if example_path is not None and example_path.exists():
        try:
            return path.read_bytes() == example_path.read_bytes()
        except OSError:
            return False
    return False


def _mark_auto_seeded_ini(path: Path) -> None:
    """Stamp a freshly-seeded settings.ini so a later merge can tell it apart
    from a user-authored one. Best-effort; failure is non-fatal."""
    try:
        import configparser
        cfg = configparser.ConfigParser()
        cfg.read(path, encoding="utf-8")
        if not cfg.has_section(_AUTO_SEED_MARKER_SECTION):
            cfg.add_section(_AUTO_SEED_MARKER_SECTION)
        import time
        cfg.set(_AUTO_SEED_MARKER_SECTION, _AUTO_SEED_MARKER_KEY,
                str(int(time.time())))
        from scanindex.infra.paths import ensure_writable
        ensure_writable(path)
        with open(path, "w", encoding="utf-8") as f:
            cfg.write(f)
    except Exception as exc:
        _log(f"could not mark auto_seeded on {path.name}: {exc}")


def _copy_file(src: Path, dst: Path) -> None:
    """Copy src→dst, creating parent dirs, making dst writable."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as fs, open(dst, "xb") as fd:
        shutil.copyfileobj(fs, fd)
    try:
        shutil.copystat(src, dst)
    except OSError:
        pass
    from scanindex.infra.paths import ensure_writable
    ensure_writable(dst)


def _safe_unlink(path: Path) -> bool:
    try:
        if path.exists():
            path.unlink()
        return True
    except OSError as exc:
        _log(f"could not remove legacy {path}: {exc}")
        return False


def _migrate_one_text_file(base_dir: Path, base: str, ext: str,
                           example_rel: str) -> Optional[str]:
    """Merge a single text resource. Returns a short status string for logging
    or None if nothing happened.

    Cases:
      (a) own exists, no legacy   → ok, nothing to do
      (b) no own, no legacy       → fresh; write NOTHING (defaults live in RAM)
      (c) no own, has legacy      → copy newest legacy → own (mark auto_seeded
                                    for .ini); leave legacy removal to caller
      (d) own + legacy            → if own looks empty/default/auto_seeded and
                                    legacy has real content, overwrite own with
                                    newest legacy; else keep own
    After a successful copy/overwrite in (c)/(d), all legacy files are removed.
    """
    own = find_versioned_file(base_dir, base, ext)
    legacy = find_legacy_files(base_dir, base, ext)
    if own is None and not legacy:
        return None  # (b) fresh — leave disk untouched

    example_path = None
    try:
        from scanindex.infra.paths import get_resource_path
        example_path = Path(get_resource_path(example_rel))
    except Exception:
        example_path = None

    if own is not None and not legacy:
        return None  # (a)

    newest = legacy[0] if legacy else None

    if own is None:
        # (c) seed own from newest legacy.
        if newest is None:
            return None
        own_rel = versioned_name(base, ext)
        own = base_dir / own_rel
        _copy_file(newest, own)
        if ext == ".ini":
            _mark_auto_seeded_ini(own)
        _log(f"seeded {own.name} from legacy {newest.name}")
    else:
        # (d) own + legacy: decide whether own should yield.
        own_is_default = (
            _is_auto_seeded_ini(own) if ext == ".ini"
            else _is_empty_or_default(own, example_path)
        )
        newest_is_real = (
            newest.exists() and newest.stat().st_size > 0
            and not _is_empty_or_default(newest, example_path)
        )
        if own_is_default and newest_is_real:
            # Overwrite own with real legacy data.
            from scanindex.infra.paths import ensure_writable
            ensure_writable(own)
            shutil.copyfile(newest, own)
            _log(f"replaced default {own.name} with legacy {newest.name}")
        else:
            # Keep own as-is; just clean legacy.
            _log(f"kept {own.name}; discarding legacy")

    # Remove all legacy files for this resource.
    for leg in legacy:
        _safe_unlink(leg)
    return own.name


def run_startup_migration() -> dict:
    """Entry point for text config files. Call as early as possible at app
    startup (before any module reads settings/sign config at import time).

    Idempotent: a second call finds own present and no legacy → no-op.
    Returns a dict mapping resource base → resulting filename (or None).
    """
    base_dir = _base_dir()
    results: dict = {}
    for base, ext, example_rel in _TEXT_FILES:
        try:
            results[base] = _migrate_one_text_file(base_dir, base, ext, example_rel)
        except Exception as exc:
            # Never let a single bad file abort the whole migration.
            _log(f"migration failed for {base}{ext}: {exc}")
            results[base] = None
    # Ensure the stamp image dir exists regardless (kept from the old seeder).
    try:
        (base_dir / "config" / "sign_stamp_images").mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return results


# ------------------------------------------------------------ DB migration

def _open_readonly_meta(db_path: Path) -> Optional[dict]:
    """Open a DB read-only-ish to read ``index_meta`` + actual document count.
    Returns None if the file isn't a valid archive DB."""
    if not db_path.exists() or db_path.stat().st_size == 0:
        return None
    try:
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        meta: dict = {}
        try:
            for r in conn.execute("SELECT key, value FROM index_meta"):
                meta[r["key"]] = r["value"]
        except sqlite3.Error:
            conn.close()
            return None
        doc_count = 0
        try:
            doc_count = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE indexed_status != 'deleted'"
            ).fetchone()[0]
        except sqlite3.Error:
            # Schema may predate the documents table — treat as 0.
            doc_count = 0
        conn.close()
        meta["__doc_count__"] = int(doc_count)
        return meta
    except sqlite3.Error:
        return None


def _wal_checkpoint(conn: sqlite3.Connection) -> bool:
    """Merge WAL pages into the main DB file so the -wal/-shm sidecars can be
    safely renamed alongside it. Returns False if the checkpoint failed, in
    which case the caller must NOT proceed with rename (data still in WAL)."""
    try:
        cur = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        row = cur.fetchone()
        # (busy, log, checkpointed) — busy != 0 means a reader blocked us.
        busy = row[0] if row else 1
        return busy == 0
    except sqlite3.Error as exc:
        _log(f"wal_checkpoint failed: {exc}")
        return False


def migrate_db_if_needed(repository_dir: Path) -> dict:
    """Merge an older repository SQLite DB into this version's filename, then
    run schema converters if the schema predates the current one.

    Safe to call repeatedly (idempotent). Does NOT touch ``pdf/`` or
    ``tantivy_index/``. Returns a dict describing what happened.

    Cases:
      1. own + no legacy → ok
      2. no own + no legacy → fresh; write NOTHING
      3. no own + legacy   → checkpoint WAL, rename newest → own, convert,
                              delete other legacy DBs
      4. own + legacy (copy-overwrite scenario) → compare actual document
                              COUNT; the file with more real rows wins; ties
                              broken by schema version then mtime.
    """
    repository_dir = Path(repository_dir).resolve()
    from scanindex.core.repository import constants as C
    from scanindex.core.repository.schema_converters import (
        convert_schema_to_latest,
        MissingConverterError,
    )

    own_name = versioned_name("repository", ".db")
    own = repository_dir / own_name
    # Legacy candidates: bare name + older-versioned names.
    legacy: list[Path] = []
    bare = repository_dir / "repository.db"
    if bare.exists() and bare != own:
        legacy.append(bare)
    if repository_dir.exists():
        for entry in repository_dir.iterdir():
            if not entry.is_file() or entry == own or entry == bare:
                continue
            if not entry.name.startswith("repository-") or not entry.name.endswith(".db"):
                continue
            ver = parse_app_version_from_name(entry)
            if ver is None:
                continue
            if _vcmp(ver, _ver_tuple(_app_version())) >= 0:
                continue
            legacy.append(entry)

    result: dict = {"action": None, "own": str(own), "legacy": [str(p) for p in legacy]}

    if own.exists() and not legacy:
        result["action"] = "noop"
        return result

    if not own.exists() and not legacy:
        result["action"] = "fresh"  # write nothing
        return result

    # Helper: rename a DB and its -wal/-shm sidecars (after checkpoint).
    def _rename_trio(src_db: Path, dst_db: Path) -> bool:
        try:
            if dst_db.exists():
                dst_db.unlink()
            src_db.rename(dst_db)
        except OSError as exc:
            _log(f"rename {src_db.name}→{dst_db.name} failed: {exc}")
            return False
        for suffix in ("-wal", "-shm"):
            s = Path(str(src_db) + suffix)
            d = Path(str(dst_db) + suffix)
            if s.exists():
                try:
                    if d.exists():
                        d.unlink()
                    s.rename(d)
                except OSError:
                    pass
        return True

    def _delete_trio(db: Path) -> None:
        for p in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
            _safe_unlink(p)

    if not own.exists():
        # CASE 3: seed own from newest legacy.
        newest = legacy[0]
        # Checkpoint the source before renaming so no data sits in its WAL.
        try:
            conn = sqlite3.connect(str(newest), isolation_level=None)
            ok = _wal_checkpoint(conn)
            conn.close()
        except sqlite3.Error:
            ok = False
        if not ok:
            _log("aborting DB migration: WAL checkpoint failed on source")
            result["action"] = "aborted_checkpoint"
            return result
        repository_dir.mkdir(parents=True, exist_ok=True)
        if not _rename_trio(newest, own):
            result["action"] = "aborted_rename"
            return result
        # Convert schema if needed.
        converted = _try_convert(own)
        # Remove other legacy DBs.
        for leg in legacy[1:]:
            _delete_trio(leg)
        result["action"] = "migrated"
        result["from"] = str(newest)
        result["converted"] = converted
        return result

    # CASE 4: own + legacy. Decide which file holds the real data.
    own_meta = _open_readonly_meta(own) or {"__doc_count__": 0, "schema_version": None}
    best = own
    best_count = int(own_meta.get("__doc_count__", 0))
    best_schema = own_meta.get("schema_version")
    best_mtime = own.stat().st_mtime if own.exists() else 0.0

    for leg in legacy:
        m = _open_readonly_meta(leg) or {"__doc_count__": 0, "schema_version": None}
        cnt = int(m.get("__doc_count__", 0))
        sch = m.get("schema_version")
        mt = leg.stat().st_mtime
        if (cnt > best_count
                or (cnt == best_count and _schema_gt(sch, best_schema))
                or (cnt == best_count and sch == best_schema and mt > best_mtime)):
            best, best_count, best_schema, best_mtime = leg, cnt, sch, mt

    if best == own:
        # Own already has the real data; just clean legacy.
        for leg in legacy:
            _delete_trio(leg)
        # Still ensure own is on current schema (e.g. own is an older-versioned
        # name from a prior release we never got around to converting).
        converted = _try_convert(own)
        result["action"] = "kept_own"
        result["converted"] = converted
        return result

    # best is a legacy file with more data. Replace own with it.
    try:
        conn = sqlite3.connect(str(best), isolation_level=None)
        ok = _wal_checkpoint(conn)
        conn.close()
    except sqlite3.Error:
        ok = False
    if not ok:
        _log("aborting DB migration: WAL checkpoint failed on legacy winner")
        result["action"] = "aborted_checkpoint"
        return result
    _delete_trio(own)
    if not _rename_trio(best, own):
        result["action"] = "aborted_rename"
        return result
    converted = _try_convert(own)
    for leg in legacy:
        if leg != best:
            _delete_trio(leg)
    result["action"] = "replaced_own"
    result["from"] = str(best)
    result["converted"] = converted
    return result


def _schema_gt(a: Optional[str], b: Optional[str]) -> bool:
    """True if schema version a > b (string compare by numeric tuple)."""
    try:
        ta = tuple(int(x) for x in (a or "0").split("."))
    except ValueError:
        ta = (0,)
    try:
        tb = tuple(int(x) for x in (b or "0").split("."))
    except ValueError:
        tb = (0,)
    la, lb = len(ta), len(tb)
    if la < lb:
        ta = ta + (0,) * (lb - la)
    elif lb < la:
        tb = tb + (0,) * (la - lb)
    return ta > tb


def _try_convert(db_path: Path):
    """Run schema converters on db_path if its schema predates the current one.
    Returns the (from,to) tuple or None. Wraps a backup so a failed conversion
    never destroys the original."""
    from scanindex.core.repository.schema_converters import (
        convert_schema_to_latest,
        MissingConverterError,
    )
    try:
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            # Backup before any conversion attempt.
            backup = Path(str(db_path) + ".preconv.bak")
            if not backup.exists():
                shutil.copy2(db_path, backup)
            converted = convert_schema_to_latest(conn)
            conn.commit()
        finally:
            conn.close()
        if converted:
            _log(f"schema converted {converted[0]}→{converted[1]} for {db_path.name}")
        return converted
    except MissingConverterError as exc:
        _log(f"schema conversion skipped ({exc}); DB left as-is")
        return None
    except Exception as exc:
        _log(f"schema conversion failed for {db_path.name}: {exc}")
        return None
