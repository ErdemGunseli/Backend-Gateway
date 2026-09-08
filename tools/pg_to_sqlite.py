"""One-time seed of a project's SQLite database from the Postgres it used to run on.

Run by `launch.sh` in the project's OWN virtualenv, from its backend directory, for a
sqlite-backed project whose secret file names a source. That placement is not
incidental: this container is the only place that can reach both a project's Postgres
and the gateway's persistent disk.

The project's own SQLAlchemy models define the target schema, so every conversion the
engines disagree about - enums, JSON, timezone-aware timestamps, booleans - goes through
SQLAlchemy's type system rather than string munging. The source side is reflected, so a
column the models have since dropped does not break the copy.

**Idempotent by data, not by file.** The target is created and inspected first: if it
already holds rows, the seed is done and the source is never contacted (so a suspended
Postgres cannot keep a converted project from booting). Only an empty target is filled.
That is deliberate - an earlier version keyed on the database file's existence, and a
seed that copied nothing still left a file behind, which then looked seeded forever
(measured 2026-09-08: Heard booted on an empty database because every table lookup
missed on a schema-qualified key).

Environment:
  SEED_FROM_DATABASE_URL   source Postgres URL          (required)
  SEED_FROM_SCHEMA         source schema                (optional; default "public")
  DATABASE_URL             target sqlite:/// URL        (set by launch.sh)
  SEED_METADATA            "module:attr" for the declarative Base
                           (default: derived from SEED_APP, else "database:Base")
  SEED_IMPORTS             comma-separated model modules to import (default "models")
  SEED_APP                 the project's ASGI target, used only to derive the above

One known, behaviour-neutral difference: a SQL NULL in a JSON column arrives as the
JSON value `null` rather than SQL NULL, because that is how SQLAlchemy's JSON type
binds None. Both read back through the ORM as None; only raw `IS NULL` SQL would tell
them apart.

Exits non-zero if anything fails or if the copy cannot be proven row-for-row, so the
launcher can refuse to start the app on a database that is empty or short.
"""

from __future__ import annotations

import importlib
import os
import sys

from sqlalchemy import MetaData, create_engine, func, insert, select


def log(msg: str) -> None:
    print("[seed] %s" % msg, flush=True)


def main() -> int:
    src_url = os.environ.get("SEED_FROM_DATABASE_URL", "")
    dst_url = os.environ["DATABASE_URL"]
    src_schema = os.environ.get("SEED_FROM_SCHEMA") or "public"
    if not src_url:
        log("SEED_FROM_DATABASE_URL is not set; nothing to seed")
        return 0

    # The models must be imported with the target settings in force: SCHEMA unset
    # (SQLite has no schemas) and DATABASE_URL already pointing at the file, because a
    # project's database module builds its engine at import time.
    os.environ.pop("SCHEMA", None)
    sys.path.insert(0, os.getcwd())

    app_target = os.environ.get("SEED_APP", "")
    pkg = app_target.split(":", 1)[0].rpartition(".")[0]
    prefix = "%s." % pkg if pkg else ""
    metadata_ref = os.environ.get("SEED_METADATA") or ("%sdatabase:Base" % prefix)
    imports = [m for m in (os.environ.get("SEED_IMPORTS") or "%smodels" % prefix).split(",") if m]

    for mod in imports:
        importlib.import_module(mod)
    mod_name, _, attr = metadata_ref.partition(":")
    base = getattr(importlib.import_module(mod_name), attr or "Base")
    target_md = base.metadata

    dst = create_engine(dst_url)
    target_md.create_all(dst)
    with dst.begin() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
    log("target ready at %s (%d tables)" % (dst_url, len(target_md.sorted_tables)))

    # Already carrying data? Then this project has been converted; do not touch the
    # source at all (it may be suspended by now) and let the app boot.
    with dst.connect() as conn:
        existing = {
            t.name: conn.execute(select(func.count()).select_from(t)).scalar_one()
            for t in target_md.sorted_tables
        }
    if any(existing.values()):
        log("target already holds %d rows; nothing to seed" % sum(existing.values()))
        return 0

    src = create_engine(src_url)
    names = [t.name for t in target_md.sorted_tables]
    # Reflect without a MetaData-level schema and key by bare table name: whether
    # SQLAlchemy stores a reflected table as "users" or "public.users" depends on the
    # schema arguments, and getting that wrong silently skips every table.
    source_md = MetaData()
    source_md.reflect(src, only=lambda n, _m: n in names, schema=src_schema)
    by_name = {t.name: t for t in source_md.tables.values()}
    log("source schema %s: %d of %d tables present" % (src_schema, len(by_name), len(names)))

    expected, copied = {}, {}
    with src.connect() as s_conn, dst.begin() as d_conn:
        for table in target_md.sorted_tables:  # parents before children
            source_table = by_name.get(table.name)
            if source_table is None:
                log("  %-28s absent in source, skipped" % table.name)
                continue
            # The source's own count, so the check below cannot be satisfied by a
            # SELECT that silently returned nothing.
            expected[table.name] = s_conn.execute(
                select(func.count()).select_from(source_table)
            ).scalar_one()
            shared = [c.name for c in table.columns if c.name in source_table.columns]
            rows = s_conn.execute(select(*[source_table.c[c] for c in shared])).fetchall()
            if rows:
                d_conn.execute(insert(table), [dict(zip(shared, r)) for r in rows])
            copied[table.name] = len(rows)
            log("  %-28s %d rows" % (table.name, len(rows)))

    if not copied:
        log("FAILED: not one of the %d model tables was found in source schema %s" % (len(names), src_schema))
        return 1

    # Verify against the target and the source, not against what we believe we sent.
    ok = True
    with dst.connect() as d_conn:
        for table in target_md.sorted_tables:
            if table.name not in copied:
                continue
            got = d_conn.execute(select(func.count()).select_from(table)).scalar_one()
            if got != expected[table.name]:
                log("  MISMATCH %s: source %d, target %d" % (table.name, expected[table.name], got))
                ok = False
    if not ok:
        log("FAILED: row counts do not match; refusing to report success")
        return 1
    if sum(expected.values()) == 0:
        log("WARNING: the source is empty; the target is empty too, which matches")
    log("seed complete: %d rows across %d tables" % (sum(copied.values()), len(copied)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
