"""Model-driven, cross-engine data pump.

Moves a project's data between database engines during a swap - Postgres (when a
project runs standalone) and SQLite (when it runs in the gateway). It is driven by
the project's OWN SQLAlchemy models, so JSON/UUID/timestamp conversions happen
through SQLAlchemy's type system automatically (the portable column types make the
schema build on either engine).

IMPORTANT: run this with the *project's* interpreter (its venv), from its backend
directory, so its models import and its SQLAlchemy version is used:

    projects/<name>/.venv/bin/python tools/db_pump.py \
        --from "postgresql://..." --to "sqlite:////data/<name>.db" \
        --metadata infrastructure.database:Base \
        --import platform.models --import product.models

Suited to the low-traffic projects the gateway hosts; it loads each table into
memory in batches rather than streaming arbitrarily large tables.
"""

from __future__ import annotations

import argparse
import importlib

from sqlalchemy import create_engine, insert, select


def _load_metadata(metadata_ref: str, import_modules: list[str]):
    # Import model modules first so every table registers on the metadata.
    for mod in import_modules:
        importlib.import_module(mod)
    module_name, _, attr = metadata_ref.partition(":")
    base = getattr(importlib.import_module(module_name), attr or "Base")
    return base.metadata


def pump(from_url: str, to_url: str, metadata, *, batch_size: int = 500) -> None:
    src = create_engine(from_url)
    dst = create_engine(to_url)

    # Build the destination schema from the same models (portable types).
    metadata.create_all(dst)

    with src.connect() as s_conn, dst.begin() as d_conn:
        # sorted_tables is FK-dependency order: parents before children.
        for table in metadata.sorted_tables:
            rows = list(s_conn.execute(select(table)).mappings())
            if not rows:
                print(f"  {table.name}: 0 rows")
                continue
            for i in range(0, len(rows), batch_size):
                d_conn.execute(insert(table), [dict(r) for r in rows[i : i + batch_size]])
            print(f"  {table.name}: {len(rows)} rows")

    # On Postgres targets, advance sequences past the copied integer PKs so future
    # inserts don't collide with preserved ids.
    if dst.dialect.name == "postgresql":
        _resync_sequences(dst, metadata)

    print("Pump complete.")


def _resync_sequences(engine, metadata) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:
        for table in metadata.sorted_tables:
            for col in table.primary_key.columns:
                if col.autoincrement and str(col.type).upper().startswith(("INT", "BIGINT", "SMALLINT")):
                    conn.execute(
                        text(
                            "SELECT setval(pg_get_serial_sequence(:t, :c), "
                            "COALESCE((SELECT MAX(" + col.name + ") FROM " + table.name + "), 1))"
                        ),
                        {"t": table.name, "c": col.name},
                    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Cross-engine data pump")
    ap.add_argument("--from", dest="from_url", required=True)
    ap.add_argument("--to", dest="to_url", required=True)
    ap.add_argument("--metadata", default="infrastructure.database:Base",
                    help="module:attr exposing the SQLAlchemy declarative Base/metadata")
    ap.add_argument("--import", dest="imports", action="append", default=[],
                    help="model module to import so its tables register (repeatable)")
    ap.add_argument("--batch-size", type=int, default=500)
    args = ap.parse_args()

    metadata = _load_metadata(args.metadata, args.imports)
    print(f"Pumping {args.from_url}  ->  {args.to_url}")
    pump(args.from_url, args.to_url, metadata, batch_size=args.batch_size)


if __name__ == "__main__":
    main()
