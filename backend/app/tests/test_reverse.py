import os
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.main import app
    with TestClient(app) as c:
        yield c


def _consume(client, item_id=2, qty=3.0):
    r = client.post("/api/consume", json={"item_id": item_id, "qty": qty})
    assert r.status_code == 200, r.text
    return r.json()


def _latest_consume_id(client):
    rows = client.get("/api/consumptions").json()
    return next(r["id"] for r in rows if r["kind"] == "consume" and not r["reversed"])


def _lot(client, lot_id):
    for r in client.get("/api/fridge").json():
        if r["id"] == lot_id:
            return r
    return None


def _lot_status(client, lot_id):
    import sqlite3
    from app.db import db_path
    con = sqlite3.connect(db_path())
    con.row_factory = sqlite3.Row
    row = dict(con.execute("SELECT status, qty_remain FROM lots WHERE id=?", (lot_id,)).fetchone())
    con.close()
    return row


def test_reverse_adds_qty_back_and_marks_history(client):
    r = _consume(client, item_id=2, qty=3)
    cid = _latest_consume_id(client)
    lot_id = r["deductions"][0]["lot_id"]
    assert _lot_status(client, lot_id)["qty_remain"] == 9

    out = client.post(f"/api/consumptions/{cid}/reverse", json={"reason": "误扣，加回"})
    assert out.status_code == 200, out.text
    assert out.json()["committed"] is True
    assert _lot_status(client, lot_id) == {"status": "on_shelf", "qty_remain": 12.0}

    hist = client.get("/api/consumptions").json()
    src = next(x for x in hist if x["id"] == cid)
    assert src["reversed"] is True and src["reason"] == "误扣，加回"
    assert any(x["kind"] == "reverse" and x["source_id"] == cid for x in hist)


def test_preview_does_not_mutate(client):
    r = _consume(client, item_id=2, qty=3)
    cid = _latest_consume_id(client)
    lot_id = r["deductions"][0]["lot_id"]

    prev = client.post(f"/api/consumptions/{cid}/reverse/preview", json={"reason": "先看看"})
    assert prev.status_code == 200
    body = prev.json()
    assert body["committed"] is False
    assert body["totals"]["mid"]["after"] - body["totals"]["mid"]["before"] == 3.0
    # Preview must leave the shelf exactly as it was.
    assert _lot_status(client, lot_id) == {"status": "on_shelf", "qty_remain": 9.0}
    hist = client.get("/api/consumptions").json()
    assert all(not x["reversed"] for x in hist if x["kind"] == "consume")


def test_non_latest_consumption_must_fail(client):
    _consume(client, item_id=2, qty=2)
    first = _latest_consume_id(client)
    _consume(client, item_id=2, qty=1)

    r = client.post(f"/api/consumptions/{first}/reverse", json={"reason": "想冲旧的"})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "not_latest"
    # Nothing was added back.
    assert _lot_status(client, 3)["qty_remain"] == 9.0


def test_missing_reason_fails(client):
    _consume(client, item_id=2, qty=1)
    cid = _latest_consume_id(client)
    for payload in ({}, {"reason": ""}, {"reason": "   "}):
        r = client.post(f"/api/consumptions/{cid}/reverse", json=payload)
        assert r.status_code == 400, (payload, r.text)
        rp = client.post(f"/api/consumptions/{cid}/reverse/preview", json=payload)
        assert rp.status_code == 400, (payload, rp.text)


def test_double_reverse_fails(client):
    _consume(client, item_id=2, qty=1)
    cid = _latest_consume_id(client)
    assert client.post(f"/api/consumptions/{cid}/reverse", json={"reason": "第一次"}).status_code == 200
    r = client.post(f"/api/consumptions/{cid}/reverse", json={"reason": "再冲一次"})
    assert r.status_code == 409 and r.json()["detail"] == "already_reversed"


def test_swept_expired_lot_is_restored_expired_not_on_shelf(client):
    # A past-expiry lot with slack: partial deduction leaves it on_shelf,
    # then the sweep retires it as expired before the reversal is confirmed.
    inb = client.post("/api/lots", json={"item_id": 1, "qty": 3, "expiry": "2026-09-01"})
    lot_id = inb.json()["id"]
    r = client.post("/api/consume", json={"item_id": 1, "qty": 1})
    assert r.status_code == 200
    assert r.json()["deductions"][0]["lot_id"] == lot_id  # earliest expiry first
    assert _lot_status(client, lot_id)["qty_remain"] == 2.0

    swept = client.post("/api/expire-sweep", json={}).json()["expired_ids"]
    assert lot_id in swept
    assert _lot_status(client, lot_id)["status"] == "expired"

    cid = _latest_consume_id(client)
    out = client.post(f"/api/consumptions/{cid}/reverse", json={"reason": "扫错了，加回"})
    assert out.status_code == 200
    p = out.json()["restores"][0]
    assert p["lot_id"] == lot_id and p["before_status"] == "expired"
    # Sticky expiry: qty comes back, status does NOT go back to on_shelf.
    assert _lot_status(client, lot_id) == {"status": "expired", "qty_remain": 3.0}
    assert _lot(client, lot_id) is None  # invisible on shelf, no "expired top bar lot"复活


def test_reverse_then_consume_reorders_by_expiry(client):
    # Clear the expired seeded lot for item 3 first.
    client.post("/api/expire-sweep", json={})
    a = client.post("/api/lots", json={"item_id": 3, "qty": 5, "expiry": "2026-12-20"}).json()["id"]
    b = client.post("/api/lots", json={"item_id": 3, "qty": 5, "expiry": "2026-11-20"}).json()["id"]

    first = client.post("/api/consume", json={"item_id": 3, "qty": 5}).json()
    assert [d["lot_id"] for d in first["deductions"]] == [b]  # earliest lot emptied
    cid = _latest_consume_id(client)
    assert client.post(f"/api/consumptions/{cid}/reverse", json={"reason": "回滚"}).status_code == 200

    again = client.post("/api/consume", json={"item_id": 3, "qty": 3})
    assert again.status_code == 200
    # Re-deduction goes through FEFO again and hits the earlier-expiry lot first.
    assert again.json()["deductions"][0]["lot_id"] == b
    assert _lot_status(client, a)["qty_remain"] == 5.0


def test_layer_totals_tie_out_after_reversal(client):
    # mid (item 2): seeded 12 on_shelf + dirty -3 lot excluded by FEFO but still
    # summed in layer totals. Consume 4, reverse it, totals must land back exactly.
    def mid_total():
        import sqlite3
        from app.db import db_path
        con = sqlite3.connect(db_path())
        v = con.execute(
            "SELECT COALESCE(SUM(qty_remain),0) FROM lots JOIN items ON items.id=lots.item_id "
            "WHERE items.layer='mid'").fetchone()[0]
        con.close()
        return v

    before = mid_total()
    client.post("/api/consume", json={"item_id": 2, "qty": 4})
    assert mid_total() == before - 4
    cid = _latest_consume_id(client)
    client.post(f"/api/consumptions/{cid}/reverse", json={"reason": "加回对账"})
    assert mid_total() == before
