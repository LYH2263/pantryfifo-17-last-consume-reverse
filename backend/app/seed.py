from app.db import connect

def init_db():
    c = connect()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, name TEXT, layer TEXT, unit TEXT);
    CREATE TABLE IF NOT EXISTS lots(
      id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INT, qty_in REAL, qty_remain REAL,
      expiry TEXT, status TEXT, data_quality TEXT
    );
    CREATE TABLE IF NOT EXISTS consumptions(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      kind TEXT DEFAULT 'consume',
      item_id INT,
      note TEXT,
      result_json TEXT,
      reversed_at TEXT,
      reverse_reason TEXT,
      reverses_id INT,
      created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    """)
    # 幂等加列：旧库（consumptions 仅 note/result_json/created_at）平滑升级
    cols = {r["name"] for r in c.execute("PRAGMA table_info(consumptions)")}
    for name, ddl in [
        ("kind", "ALTER TABLE consumptions ADD COLUMN kind TEXT DEFAULT 'consume'"),
        ("item_id", "ALTER TABLE consumptions ADD COLUMN item_id INT"),
        ("reversed_at", "ALTER TABLE consumptions ADD COLUMN reversed_at TEXT"),
        ("reverse_reason", "ALTER TABLE consumptions ADD COLUMN reverse_reason TEXT"),
        ("reverses_id", "ALTER TABLE consumptions ADD COLUMN reverses_id INT"),
    ]:
        if name not in cols:
            c.execute(ddl)
    c.commit()
    if c.execute("SELECT COUNT(*) c FROM items").fetchone()["c"] == 0:
        c.executemany("INSERT INTO items(name,layer,unit) VALUES (?,?,?)", [
            ("牛奶", "upper", "盒"), ("鸡蛋", "mid", "个"), ("冻饺", "lower", "袋"),
        ])
        c.executemany(
            "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
            [
                (1, 2, 2, "2026-10-01", "on_shelf", "clean"),
                (1, 1, 1, "2026-09-28", "on_shelf", "clean"),
                (2, 12, 12, "2026-11-01", "on_shelf", "clean"),
                (3, 1, 1, "2025-01-01", "on_shelf", "dirty"),
                (2, -3, -3, "2026-12-01", "on_shelf", "dirty"),
            ],
        )
        c.execute("INSERT INTO settings(key,value) VALUES ('warn_days','3')")
        c.commit()
    c.close()
