"""One-time seed of a project's SQLite database from the Postgres it used to run on.

Run by `launch.sh` in the project's OWN virtualenv, from its backend directory, when a
sqlite-backed project has no database file yet and its secret file names a source. That
placement is not incidental: this container is the only place that can reach both a
project's Postgres and the gateway's persistent disk.

The project's own SQLAlchemy models define the target schema, so every conversion the
engines disagree about - enums, JSON, timezone-aware timestamps, booleans - goes through
SQLAlchemy's type system rather than string munging. The source side is reflected, so a
column the models have since dropped does not break the copy.

Environment:
  SEED_FROM_DATABASE_URL   source Postgres URL          (required)
  SEED_FROM_SCHEMA         source schema                (optional; default "public")
  DATABASE_URL             target sqlite:/// URL        (set by launch.sh)
  SEED_METADATA            "module:attr" for the declarative Base
                           (default: derived from SEED_APP, else "database:Base")
  SEED_IMPORTS             comma-separated model modules to import (default "models")
  SEED_APP                 the project's ASGI target, used only to derive the above

Exits non-zero if anything fails, so the launcher can refuse to start the app on an
empty database rather than silently serving one.
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

    # The models must be imported with the target settings in force: SCHEMA unset (SQLite
    # has no schemas) and DATABASE_URL already pointing at the file, because a project's
    # database module builds its engine at import time.
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
    log("target schema created at %s (%d tables)" % (dst_url, len(target_md.sorted_tables)))

    src = create_engine(src_url)
    names = [t.name for t in target_md.sorted_tables]
    source_md = MetaData(schema=None if src_schema == "public" else src_schema)
    source_md.reflect(src, only=lambda n, _m: n in names, schema=src_schema)
    log("source %s reflected: %d of %d tables present" % (src_schema, len(source_md.tables), len(names)))

    copied = {}
    with src.connect() as s_conn, dst.begin() as d_conn:
        for table in target_md.sorted_tables:  # parents before children
            key = table.name if src_schema == "public" else "%s.%s" % (src_schema, table.name)
            # `or` would truth-test a Table, which SQLAlchemy refuses to define.
            source_table = source_md.tables.get(key)
            if source_table is None:
                source_table = source_md.tables.get(table.name)
            if source_table is None:
                log("  %-28s absent in source, skipped" % table.name)
                continue
            shared = [c.name for c in table.columns if c.name in source_table.columns]
            rows = s_conn.execute(select(*[source_table.c[c] for c in shared])).fetchall()
            if rows:
                d_conn.execute(insert(table), [dict(zip(shared, r)) for r in rows])
            copied[table.name] = len(rows)
            log("  %-28s %d rows" % (table.name, len(rows)))

    # Verify against the target, not against what we believe we sent.
    ok = True
    with dst.connect() as d_conn, src.connect() as s_conn:
        for table in target_md.sorted_tables:
            if table.name not in copied:
                continue
            got = d_conn.execute(select(func.count()).select_from(table)).scalar_one()
            if got != copied[table.name]:
                log("  MISMATCH %s: source %d, target %d" % (table.name, copied[table.name], got))
                ok = False
    if not ok:
        log("FAILED: row counts do not match; refusing to report success")
        return 1
    log("seed complete: %d rows across %d tables" % (sum(copied.values()), len(copied)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
