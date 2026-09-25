"""A small SQL shell for the tracker's database, for practicing SQL without installing anything.

  tracker\\.venv\\Scripts\\python tracker\\sql.py            the sandbox copy (safe to break)
  tracker\\.venv\\Scripts\\python tracker\\sql.py --live     the live database, read-only
  tracker\\.venv\\Scripts\\python tracker\\sql.py "SELECT ..."   run one query and exit

Type SQL ending with ; (it can span lines). Shell commands:
  .tables          list the tables (and any views you've made)
  .schema [name]   show how a table was created
  .reset           replace the sandbox with a fresh copy of the live database
  .quit            leave
"""

import sqlite3
import sys

import db

MAX_ROWS = 40


def show(cursor):
    if cursor.description is None:  # INSERT, CREATE VIEW, ...
        print(f"ok ({cursor.rowcount} rows changed)" if cursor.rowcount >= 0 else "ok")
        return
    headers = [d[0] for d in cursor.description]
    rows = cursor.fetchmany(MAX_ROWS + 1)
    more = len(rows) > MAX_ROWS
    rows = [["NULL" if v is None else f"{v:.3f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v) for v in r]
            for r in rows[:MAX_ROWS]]
    widths = [min(max([len(h)] + [len(r[i]) for r in rows]), 40) for i, h in enumerate(headers)]
    line = lambda cells: " | ".join(c[:w].ljust(w) for c, w in zip(cells, widths))
    print(line(headers))
    print("-+-".join("-" * w for w in widths))
    for r in rows:
        print(line(r))
    print(f"({len(rows)} rows{', more not shown: add LIMIT/WHERE' if more else ''})")


def command(con, text):
    parts = text.split()
    if parts[0] == ".tables":
        for (name, kind) in con.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') "
                                        "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"):
            print(f"{name}  ({kind})")
    elif parts[0] == ".schema":
        query = "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL" + (" AND name = ?" if len(parts) > 1 else "")
        for (sql,) in con.execute(query, parts[1:2]):
            print(sql + ";\n")
    else:
        print(__doc__)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    live = "--live" in sys.argv
    if live:
        con = sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True)
        print(f"live database, read-only: {db.DB_PATH}")
    else:
        if not db.SANDBOX.exists():
            db.make_sandbox()
        con = sqlite3.connect(db.SANDBOX)
        print(f"sandbox: {db.SANDBOX}  (.reset for a fresh copy, .quit to leave)")
    if args:
        show(con.execute(args[0]))
        return
    buffer = ""
    while True:
        try:
            line = input("sql> " if not buffer else "...> ")
        except (EOFError, KeyboardInterrupt):
            break
        if not buffer and line.strip().startswith("."):
            if line.strip() == ".quit":
                break
            if line.strip() == ".reset" and not live:
                con.close()
                db.make_sandbox()
                con = sqlite3.connect(db.SANDBOX)
                print("fresh sandbox copy")
                continue
            command(con, line.strip())
            continue
        buffer += line + "\n"
        if sqlite3.complete_statement(buffer):
            try:
                show(con.execute(buffer))
                con.commit()
            except sqlite3.Error as e:
                print(f"error: {e}")
            buffer = ""


if __name__ == "__main__":
    main()
