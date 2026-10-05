import threading
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import connect


def exp(days):
    return (date.today() + timedelta(days=days)).isoformat()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    from app.main import app
    with TestClient(app) as c:
        yield c


# ---- 小工具 ----

def consume(client, item_id, qty, note=""):
    return client.post("/api/consume", json={"item_id": item_id, "qty": qty, "note": note})

def inbound(client, item_id, qty, expiry):
    return client.post("/api/lots", json={"item_id": item_id, "qty": qty, "expiry": expiry})

def reverse_confirm(client, consumption_id=None, reason="x"):
    body = {"reason": reason}
    if consumption_id is not None:
        body["consumption_id"] = consumption_id
    return client.post("/api/reverse", json=body)

def lot_qty(lot_id):
    c = connect()
    row = dict(c.execute("SELECT qty_remain,status FROM lots WHERE id=?", (lot_id,)).fetchone())
    c.close()
    return row

def n_consumptions(kind=None):
    c = connect()
    if kind:
        n = c.execute("SELECT COUNT(*) c FROM consumptions WHERE kind=?", (kind,)).fetchone()["c"]
    else:
        n = c.execute("SELECT COUNT(*) c FROM consumptions").fetchone()["c"]
    c.close()
    return n

def fridge_lot(client, lot_id):
    for r in client.get("/api/fridge").json():
        if r["id"] == lot_id:
            return r
    return None


# ---- T01 鸡蛋（未来期）完整链路：预览不动 -> 加回 -> 履历标记 ----

def test_t01_happy_preview_then_confirm(client):
    # seed lot3 鸡蛋 12 @2026-11-01（相对今天为未来）
    r = consume(client, 2, 3)
    assert r.status_code == 200
    assert lot_qty(3)["qty_remain"] == 9

    pv = client.post("/api/reverse/preview", json={}).json()
    assert pv["valid"] is True and pv["error"] is None
    rst = pv["restorations"][0]
    assert rst["lot_id"] == 3 and rst["qty_before"] == 9 and rst["qty_after"] == 12
    assert rst["status_after"] == "on_shelf"
    # 预览零写入：余量与履历条数都不变
    assert lot_qty(3)["qty_remain"] == 9
    assert n_consumptions() == 1

    ok = reverse_confirm(client, reason="多扣了").json()
    assert ok["ok"] is True and ok["reversed_id"] == pv["target"]["id"]
    assert lot_qty(3)["qty_remain"] == 12
    assert lot_qty(3)["status"] == "on_shelf"

    hist = client.get("/api/consumptions").json()
    rev_row = next(h for h in hist if h["kind"] == "reverse")
    con_row = next(h for h in hist if h["kind"] == "consume")
    assert rev_row["reverses_id"] == con_row["id"]
    assert rev_row["note"] == "多扣了" and rev_row["item_name"] == "鸡蛋"
    assert con_row["reversed_at"] and con_row["reversal_id"] == rev_row["id"]
    assert all(h["reversible"] is False for h in hist)


# ---- T02 原因字 ----

def test_t02_reason_required(client):
    consume(client, 2, 1)
    for bad in ("", "   "):
        r = reverse_confirm(client, reason=bad)
        assert r.status_code == 400 and r.json()["detail"] == "reason_required"
        assert n_consumptions("reverse") == 0


# ---- T03 空链 ----

def test_t03_no_consumption(client):
    pv = client.post("/api/reverse/preview", json={}).json()
    assert pv["valid"] is False and pv["error"] == "no_consumption"
    assert client.post("/api/reverse", json={"reason": "x"}).status_code == 409


# ---- T04 重复冲正 ----

def test_t04_double_reverse(client):
    consume(client, 2, 1)
    cid = client.get("/api/consumptions").json()[0]["id"]
    assert reverse_confirm(client, consumption_id=cid, reason="第一次").status_code == 200
    second = reverse_confirm(client, consumption_id=cid, reason="再冲一次")
    assert second.status_code == 409 and second.json()["detail"] == "already_reversed"
    pv = client.post("/api/reverse/preview", json={"consumption_id": cid}).json()
    assert pv["valid"] is False and pv["error"] == "already_reversed"
    # 不带 id 时链上已无可逆笔
    assert reverse_confirm(client, reason="再冲一次").status_code == 409
    assert n_consumptions("reverse") == 1


# ---- T05 非最近一笔失败；冲掉最新后旧笔变可逆 ----

def test_t05_not_latest_then_chain_unwinds(client):
    consume(client, 2, 1)   # A 鸡蛋，扣 lot3
    consume(client, 1, 1)   # B 牛奶（最新）
    hist = client.get("/api/consumptions").json()
    a_id = next(h["id"] for h in hist if h["item_id"] == 2)
    b_id = next(h["id"] for h in hist if h["item_id"] == 1)

    r = reverse_confirm(client, consumption_id=a_id, reason="冲旧的")
    assert r.status_code == 409 and r.json()["detail"] == "not_latest"
    pv = client.post("/api/reverse/preview", json={"consumption_id": a_id}).json()
    assert pv["valid"] is False and pv["error"] == "not_latest"

    assert reverse_confirm(client, consumption_id=b_id, reason="冲牛奶").status_code == 200
    hist = client.get("/api/consumptions").json()
    a = next(h for h in hist if h["id"] == a_id)
    assert a["reversible"] is True
    assert reverse_confirm(client, consumption_id=a_id, reason="冲鸡蛋").status_code == 200
    assert lot_qty(3)["qty_remain"] == 12  # 鸡蛋全量回到 seed


# ---- T06 核心交织：消费后 sweep 下架，冲正余量补回但不回架 ----

def test_t06_swept_between_consume_and_reverse(client):
    a = inbound(client, 2, 1, exp(-7)).json()["id"]   # 最早，先被扣光
    b = inbound(client, 2, 2, exp(-1)).json()["id"]
    r = consume(client, 2, 1.5).json()
    assert [d["lot_id"] for d in r["deductions"]] == [a, b]
    assert lot_qty(a)["status"] == "consumed" and lot_qty(a)["qty_remain"] == 0
    assert lot_qty(b)["qty_remain"] == 1.5  # b 入库 2，只被扣 0.5

    swept = client.post("/api/expire-sweep", json={}).json()["expired_ids"]
    assert b in swept and a not in swept  # consumed 不被 sweep 触碰

    pv = client.post("/api/reverse/preview", json={}).json()
    assert pv["valid"] is True
    by_lot = {x["lot_id"]: x for x in pv["restorations"]}
    assert by_lot[a]["status_after"] == "expired" and by_lot[a]["qty_after"] == 1
    assert by_lot[b]["status_after"] == "expired" and by_lot[b]["qty_after"] == 2
    # 确认前全层余量未变
    assert fridge_lot(client, a) is None and fridge_lot(client, b) is None

    assert reverse_confirm(client, reason="顾客退货").status_code == 200
    assert lot_qty(a) == {"qty_remain": 1, "status": "expired"}
    assert lot_qty(b) == {"qty_remain": 2, "status": "expired"}
    assert fridge_lot(client, a) is None and fridge_lot(client, b) is None
    alert_ids = [x["id"] for x in client.get("/api/alerts").json()]
    assert a not in alert_ids and b not in alert_ids  # 顶条不会把补回批当在架过期


# ---- T07 未 sweep 但到期日已到/已过：冲正直接落 expired ----

def test_t07_expiry_crossed_without_sweep(client):
    lid = inbound(client, 2, 2, exp(0)).json()["id"]  # 当日到期，FEFO 最先
    consume(client, 2, 1)
    # 不调 sweep；批次仍 on_shelf，但冲正必须按当日到期重判
    assert reverse_confirm(client, reason="x").status_code == 200
    assert lot_qty(lid) == {"qty_remain": 2, "status": "expired"}


def test_t07b_future_lot_restored_on_shelf(client):
    lid = inbound(client, 2, 2, exp(10)).json()["id"]
    consume(client, 2, 1)
    reverse_confirm(client, reason="x")
    assert lot_qty(lid) == {"qty_remain": 2, "status": "on_shelf"}


# ---- T08 同批两次消费，倒序逐笔全回退 ----

def test_t08_sequential_consumes_full_rewind(client):
    consume(client, 2, 5)  # 12 -> 7
    consume(client, 2, 3)  # 7 -> 4
    assert lot_qty(3)["qty_remain"] == 4
    hist = client.get("/api/consumptions").json()
    old_id = min(h["id"] for h in hist if h["kind"] == "consume")
    assert reverse_confirm(client, consumption_id=old_id, reason="x").status_code == 409
    reverse_confirm(client, reason="冲第二笔")
    assert lot_qty(3)["qty_remain"] == 7
    reverse_confirm(client, reason="冲第一笔")
    assert lot_qty(3)["qty_remain"] == 12


# ---- T09 跨批消费交织后的回退对账 ----

def test_t09_straddle_two_lots(client):
    c_lot = inbound(client, 2, 4, exp(14)).json()["id"]
    consume(client, 2, 10)  # lot3 12 -> 2
    consume(client, 2, 4)   # 剩余在架两批分摊
    reverse_confirm(client, reason="冲新")
    reverse_confirm(client, reason="冲旧")
    assert lot_qty(3)["qty_remain"] == 12
    assert lot_qty(c_lot)["qty_remain"] == 4


# ---- T10 预览零写入（整库快照） ----

def test_t10_preview_writes_nothing(client):
    consume(client, 2, 1)
    c = connect()
    before = ([dict(r) for r in c.execute("SELECT * FROM lots")],
              c.execute("SELECT COUNT(*) n FROM consumptions").fetchone()["n"])
    c.close()
    client.post("/api/reverse/preview", json={})
    c = connect()
    after = ([dict(r) for r in c.execute("SELECT * FROM lots")],
             c.execute("SELECT COUNT(*) n FROM consumptions").fetchone()["n"])
    c.close()
    assert before == after


# ---- T11 冲正后再消费，FEFO 扣减顺序与首次完全一致 ----

def test_t11_fefo_order_identical_after_reverse(client):
    consume(client, 2, 12)  # 先清空 seed lot3，隔离自建两批
    d = inbound(client, 2, 2, exp(5)).json()["id"]
    e = inbound(client, 2, 5, exp(20)).json()["id"]
    first = consume(client, 2, 4).json()["deductions"]
    assert [x["lot_id"] for x in first] == [d, e] and first[0]["take"] == 2 and first[1]["take"] == 2
    reverse_confirm(client, reason="重扫错误")
    second = consume(client, 2, 4).json()["deductions"]
    assert [(x["lot_id"], x["take"]) for x in second] == [(x["lot_id"], x["take"]) for x in first]
    assert lot_qty(d)["status"] == "consumed"
    assert lot_qty(e)["qty_remain"] == 3


# ---- T12 当日到期 sweep / reverse 语义一致 ----

def test_t12_sweep_boundary_today(client):
    today_lot = inbound(client, 2, 1, exp(0)).json()["id"]
    tomorrow_lot = inbound(client, 2, 1, exp(1)).json()["id"]
    ids = client.post("/api/expire-sweep", json={}).json()["expired_ids"]
    assert today_lot in ids and tomorrow_lot not in ids


# ---- T13 旧形状履历行（kind/item_id 均 NULL）可解析、可冲 ----

def test_t13_legacy_row_name_fallback_and_reversible(client):
    import json as _json
    c = connect()
    c.execute("INSERT INTO consumptions(kind,item_id,note,result_json,created_at) VALUES (NULL,NULL,?,?,?)",
              ("old", _json.dumps({"ok": True, "deductions": [{"lot_id": 3, "take": 1, "expiry": exp(20)}],
                                   "short": 0.0}), "2026-09-01T00:00:00+00:00"))
    c.commit()
    c.close()
    hist = client.get("/api/consumptions").json()
    row = hist[0]
    assert row["kind"] == "consume" and row["item_name"] == "鸡蛋" and row["reversible"] is True
    assert reverse_confirm(client, reason="补旧账").status_code == 200
    assert lot_qty(3)["qty_remain"] == 13


# ---- T14 旧四列库启动时自动迁移 ----

def test_t14_migrate_old_schema(tmp_path, monkeypatch):
    import sqlite3
    db_dir = tmp_path / "legacy"
    db_dir.mkdir()
    monkeypatch.setenv("DATA_DIR", str(db_dir))
    from app.db import db_path
    raw = sqlite3.connect(db_path())
    raw.executescript("""
      CREATE TABLE items(id INTEGER PRIMARY KEY, name TEXT, layer TEXT, unit TEXT);
      CREATE TABLE lots(id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INT, qty_in REAL,
        qty_remain REAL, expiry TEXT, status TEXT, data_quality TEXT);
      CREATE TABLE consumptions(id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT, result_json TEXT, created_at TEXT);
      CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
      INSERT INTO items VALUES (1,'鸡蛋','mid','个');
      INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality)
        VALUES (1,5,5,'2099-01-01','on_shelf','clean');
      INSERT INTO settings VALUES ('warn_days','3');
      INSERT INTO consumptions(note,result_json,created_at)
        VALUES ('legacy','{"ok":true,"deductions":[]}','2026-01-01T00:00:00+00:00');
    """)
    raw.commit(); raw.close()

    from app.main import app
    with TestClient(app) as c2:
        hist = c2.get("/api/consumptions").json()
        assert hist[0]["kind"] == "consume"  # DEFAULT 'consume' 回填
        r = consume(c2, 1, 1)
        assert r.status_code == 200
        assert reverse_confirm(c2, reason="迁移后冲正").status_code == 200


# ---- T15 失败消费不写履历、不影响可逆链 ----

def test_t15_short_consume_writes_nothing(client):
    r = consume(client, 2, 999)
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "short"
    assert n_consumptions() == 0
    pv = client.post("/api/reverse/preview", json={}).json()
    assert pv["valid"] is False and pv["error"] == "no_consumption"


# ---- T16 并发确认同一笔：恰一成一败，恰一条 reverse 行 ----

def test_t16_concurrent_confirm(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    from app.main import app
    with TestClient(app) as setup_c:
        assert consume(setup_c, 2, 1).status_code == 200
        data_dir = str(tmp_path / "data")

    def worker(out, idx):
        # 每个线程自己的连接/客户端，避免 httpx client 跨线程共用
        import os
        os.environ["DATA_DIR"] = data_dir
        c = TestClient(app)
        out[idx] = c.post("/api/reverse", json={"reason": "并发冲正"}).status_code

    results = [None, None]
    t1 = threading.Thread(target=worker, args=(results, 0))
    t2 = threading.Thread(target=worker, args=(results, 1))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results) == [200, 409]
    assert n_consumptions("reverse") == 1
    assert lot_qty(3)["qty_remain"] == 12
