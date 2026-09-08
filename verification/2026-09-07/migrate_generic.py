"""Additively copy a product's rows from a standalone database into the gateway's
schema on the shared Gateway DB.

Properties that make this safe to run against a live database:
  * additive only - it never updates or deletes an existing row;
  * a source user whose email already exists in the target is not duplicated, and its
    child rows are re-pointed at the row already there;
  * every primary key is remapped past the target's current maximum, so the two eras
    of ids cannot collide;
  * a child row whose parent was not copied is dropped rather than left dangling;
  * id sequences are reset afterwards, so the app's next insert does not collide;
  * MIG_DRY=1 reports exactly what it would do and writes nothing.

Driven by environment variables so no credential ever appears in the job command:
  DATABASE_URL, or the variable named by MIG_SRC_URL_FROM   source database
  CMP_GATEWAY_DB               target database        MIG_TGT_SCHEMA   e.g. "in_sight"
  MIG_SRC_SCHEMAS  comma-separated, e.g. "public,in_sight"
  MIG_TABLES  ";"-separated, parents first, each "table" or
              "table:col=parent|col=parent" naming every foreign key to remap.
"""
import ast
import json as jsonlib
import os
import psycopg2
from psycopg2.extras import Json

REPAIRED = []


def adapt(value, is_json_col, where):
    """Make a source value insertable into the target column.

    Two eras of schema meet here: psycopg2 reads a JSON column back as a dict/list and
    cannot insert one directly, and the older standalone database stores some values as
    text holding a PYTHON repr ({'a': 1}) where the live schema now has a json column.
    Both are converted; anything still unparseable is stored as a JSON string rather
    than dropped, and reported.
    """
    if isinstance(value, (dict, list)):
        return Json(value)
    if is_json_col and isinstance(value, str) and value:
        try:
            return Json(jsonlib.loads(value))
        except Exception:
            pass
        try:
            return Json(ast.literal_eval(value))
        except Exception:
            REPAIRED.append(where)
            return Json(value)
    return value


DRY = os.environ.get("MIG_DRY", "1") == "1"
SRC = os.environ.get(os.environ.get("MIG_SRC_URL_FROM", ""), "") or os.environ["DATABASE_URL"]
DST = os.environ["CMP_GATEWAY_DB"]
TGT = os.environ["MIG_TGT_SCHEMA"]
SRC_SCHEMAS = [s for s in os.environ["MIG_SRC_SCHEMAS"].split(",") if s]

SPEC = []          # [(table, [(column, parent_table), ...]), ...]
for entry in [t for t in os.environ["MIG_TABLES"].split(";") if t]:
    name, _, parents = entry.partition(":")
    SPEC.append((name, [p.split("=") for p in parents.split("|") if p]))

src = psycopg2.connect(SRC, connect_timeout=20)
dst = psycopg2.connect(DST, connect_timeout=20)
sc, dc = src.cursor(), dst.cursor()


def cols(cur, schema, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
        (schema, table),
    )
    return [r[0] for r in cur.fetchall()]


def quoted(names):
    return ",".join('"%s"' % n for n in names)


def exists(schema, table):
    sc.execute("SELECT to_regclass(%s)", ("%s.%s" % (schema, table),))
    return bool(sc.fetchone()[0])


totals = {}
print("MIG target schema %s on the gateway database; source schemas %s" % (TGT, SRC_SCHEMAS))
for table, _ in SPEC:
    dc.execute('SELECT count(*) FROM "%s"."%s"' % (TGT, table))
    print("MIG before: %s.%s rows=%d" % (TGT, table, dc.fetchone()[0]))

# Read once, then accumulate in memory across every source schema. Re-reading it per
# schema would make a dry run disagree with the real run: with nothing written yet, a
# person present in two source schemas would look new the second time round.
dc.execute('SELECT lower(email), id FROM "%s".users' % TGT)
by_email = {r[0]: r[1] for r in dc.fetchall()}

for schema in SRC_SCHEMAS:
    if not exists(schema, SPEC[0][0]):
        print("MIG source schema %s: no %s table, skipped" % (schema, SPEC[0][0]))
        continue
    print("MIG --- reading source schema %s" % schema)
    idmaps = {}

    for table, parents in SPEC:
        if not exists(schema, table):
            continue
        scols = cols(sc, schema, table)
        tcols = [c for c in scols if c in cols(dc, TGT, table)]
        is_users = table == "users" and "email" in scols
        dc.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s",
            (TGT, table),
        )
        json_cols = {r[0] for r in dc.fetchall() if r[1] in ("json", "jsonb")}

        dc.execute('SELECT coalesce(max(id),0) FROM "%s"."%s"' % (TGT, table))
        next_id = dc.fetchone()[0] + 1
        sc.execute('SELECT %s FROM "%s"."%s" ORDER BY id' % (quoted(scols), schema, table))
        rows = sc.fetchall()
        idmap, copied, deduped, orphaned = {}, 0, 0, 0

        for row in rows:
            rec = dict(zip(scols, row))
            old_id = rec["id"]

            if is_users:
                email = (rec.get("email") or "").lower()
                if email in by_email:
                    idmap[old_id] = by_email[email]   # child rows follow the existing account
                    deduped += 1
                    continue

            dangling = False
            for parent_col, parent_table in parents:
                if parent_col in rec and rec[parent_col] is not None:
                    pmap = idmaps.get(parent_table, {})
                    if rec[parent_col] not in pmap:
                        dangling = True
                        break
                    rec[parent_col] = pmap[rec[parent_col]]
            if dangling:
                orphaned += 1
                continue

            rec["id"] = next_id + copied
            idmap[old_id] = rec["id"]
            if is_users:
                by_email[(rec.get("email") or "").lower()] = rec["id"]
            if not DRY:
                dc.execute(
                    'INSERT INTO "%s"."%s" (%s) VALUES (%s)'
                    % (TGT, table, quoted(tcols), ",".join(["%s"] * len(tcols))),
                    [adapt(rec[c], c in json_cols, "%s.%s.%s" % (schema, table, c)) for c in tcols],
                )
            copied += 1

        idmaps[table] = idmap
        totals[table] = totals.get(table, 0) + copied
        note = ""
        if deduped:
            note += " (%d already in target, children re-pointed)" % deduped
        if orphaned:
            note += " (%d skipped: parent not copied)" % orphaned
        print("MIG %s.%s -> %d row(s)%s" % (schema, table, copied, note))

    if not DRY:
        dst.commit()

if not DRY:
    for table, _ in SPEC:
        dc.execute(
            "SELECT setval(pg_get_serial_sequence('\"%s\".\"%s\"','id'), "
            "GREATEST((SELECT coalesce(max(id),1) FROM \"%s\".\"%s\"),1))" % (TGT, table, TGT, table)
        )
    dst.commit()
    for table, _ in SPEC:
        dc.execute('SELECT count(*) FROM "%s"."%s"' % (TGT, table))
        print("MIG after:  %s.%s rows=%d" % (TGT, table, dc.fetchone()[0]))

if REPAIRED:
    print("MIG values stored as JSON strings (unparseable in source): %s" % sorted(set(REPAIRED)))
print("MIG %s totals: %s" % ("DRY RUN (nothing written)" if DRY else "APPLIED", totals))
print("MIG=== END")
