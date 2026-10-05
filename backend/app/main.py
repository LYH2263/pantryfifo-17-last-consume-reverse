import json
from datetime import date, datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.engines.fefo import consume_fefo, expire_lots, project_restores

app = FastAPI(title="Pantryfifo", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

@app.get("/api/health")
def health(): return {"ok": True, "project": "pantryfifo"}

@app.get("/api/items")
def items():
    c = connect(); rows = [dict(r) for r in c.execute("SELECT * FROM items")]; c.close(); return rows

@app.get("/api/fridge")
def fridge(layer: str | None = None):
    c = connect()
    q = """SELECT lots.*, items.name, items.layer, items.unit FROM lots
           JOIN items ON items.id=lots.item_id WHERE lots.status='on_shelf'"""
    args = []
    if layer:
        q += " AND items.layer=?"; args.append(layer)
    rows = [dict(r) for r in c.execute(q, args)]; c.close(); return rows

def layer_totals(c) -> dict:
    """Sum of qty_remain per layer over ALL lots (any status), for preview/check."""
    rows = c.execute(
        """SELECT items.layer AS layer, COALESCE(SUM(lots.qty_remain),0) AS total
           FROM lots JOIN items ON items.id=lots.item_id GROUP BY items.layer""")
    return {r["layer"]: round(r["total"], 6) for r in rows}

@app.get("/api/alerts")
def alerts():
    c = connect()
    warn = int(c.execute("SELECT value FROM settings WHERE key='warn_days'").fetchone()["value"])
    today = date.today().isoformat()
    rows = [dict(r) for r in c.execute(
        """SELECT lots.*, items.name, items.layer FROM lots JOIN items ON items.id=lots.item_id
           WHERE status='on_shelf' AND qty_remain>0 AND expiry IS NOT NULL""")]
    c.close()
    out = []
    for r in rows:
        if r["expiry"] <= today:
            r["level"] = "expired"
            out.append(r)
        else:
            # simple day diff via fromisoformat
            delta = (date.fromisoformat(r["expiry"]) - date.today()).days
            if delta <= warn:
                r["level"] = "soon"; r["days_left"] = delta; out.append(r)
    return out

class LotIn(BaseModel):
    item_id: int
    qty: float
    expiry: str

@app.post("/api/lots")
def inbound(body: LotIn):
    c = connect()
    item = c.execute("SELECT id FROM items WHERE id=?", (body.item_id,)).fetchone()
    if not item: c.close(); raise HTTPException(404, "item")
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
        (body.item_id, body.qty, body.qty, body.expiry, "on_shelf", "clean"))
    c.commit(); lid = cur.lastrowid; c.close(); return {"id": lid}

class ConsumeIn(BaseModel):
    item_id: int
    qty: float
    note: str = ""

@app.post("/api/consume")
def consume(body: ConsumeIn):
    c = connect()
    c.execute("BEGIN IMMEDIATE")
    lots = [dict(r) for r in c.execute(
        "SELECT * FROM lots WHERE item_id=? AND status='on_shelf' AND qty_remain>0", (body.item_id,))]
    result = consume_fefo(lots, body.qty)
    if not result["ok"] and result["reason"] == "qty_non_positive":
        c.rollback(); c.close(); raise HTTPException(400, result["reason"])
    if not result["ok"]:
        c.rollback(); c.close(); raise HTTPException(409, result)
    for d in result["deductions"]:
        c.execute("UPDATE lots SET qty_remain = qty_remain - ? WHERE id=?", (d["take"], d["lot_id"]))
        rem = c.execute("SELECT qty_remain FROM lots WHERE id=?", (d["lot_id"],)).fetchone()["qty_remain"]
        if rem <= 0:
            c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?", (d["lot_id"],))
    c.execute(
        "INSERT INTO consumptions(note,result_json,created_at,kind,item_id) VALUES (?,?,?,?,?)",
        (body.note, json.dumps(result), datetime.now(timezone.utc).isoformat(), "consume", body.item_id))
    c.commit(); c.close(); return result

@app.post("/api/expire-sweep")
def expire_sweep():
    c = connect()
    c.execute("BEGIN IMMEDIATE")
    lots = [dict(r) for r in c.execute("SELECT * FROM lots WHERE status='on_shelf'")]
    ids = expire_lots(lots, date.today().isoformat())
    for i in ids:
        c.execute("UPDATE lots SET status='expired' WHERE id=?", (i,))
    c.commit(); c.close(); return {"expired_ids": ids}

def _consumption_row(c, cid: int):
    return c.execute("SELECT * FROM consumptions WHERE id=?", (cid,)).fetchone()

class ReverseIn(BaseModel):
    reason: str = ""

@app.get("/api/consumptions")
def list_consumptions():
    c = connect()
    rows = [dict(r) for r in c.execute(
        """SELECT c.*, items.name AS item_name
           FROM consumptions c LEFT JOIN items ON items.id=c.item_id
           ORDER BY c.id DESC""")]
    c.close()
    for r in rows:
        try: r["result"] = json.loads(r.pop("result_json") or "{}")
        except json.JSONDecodeError: r["result"] = {}
        r["reversed"] = bool(r.get("reversed_at"))
    return rows

def _latest_open_consumption_id(c) -> int | None:
    """Most recent consumption event that has not been reversed yet.

    Only rows of kind='consume' count; a reversal row of kind='reverse' sits
    after its source in id order but never blocks a newer consume.
    """
    r = c.execute(
        "SELECT MAX(id) AS id FROM consumptions WHERE kind='consume' AND reversed_at IS NULL"
    ).fetchone()
    return r["id"] if r and r["id"] is not None else None

def _reverse_projection(c, cid: int, reason: str, commit: bool):
    """Shared preview/commit path: one add-back projection over current lots."""
    today = date.today().isoformat()
    row = _consumption_row(c, cid)
    if row is None:
        raise HTTPException(404, "consumption_not_found")
    row = dict(row)
    if row.get("kind") != "consume":
        raise HTTPException(409, "not_a_consumption")
    if row.get("reversed_at"):
        raise HTTPException(409, "already_reversed")
    if not reason.strip():
        raise HTTPException(400, "reason_required")
    latest = _latest_open_consumption_id(c)
    if latest != cid:
        # Either a newer successful deduction exists, or this one was already
        # reversed (its reversal row makes a newer consume the "latest open").
        raise HTTPException(409, {"reason": "not_latest", "latest_id": latest})
    try:
        result = json.loads(row.get("result_json") or "{}")
    except json.JSONDecodeError:
        result = {}
    restores = result.get("deductions", [])
    lot_rows = [dict(r) for r in c.execute("SELECT * FROM lots")]
    before_totals = layer_totals(c)
    projected = project_restores(lot_rows, restores, today)
    after_totals = dict(before_totals)
    for p in projected:
        lot = next(r for r in lot_rows if r["id"] == p["lot_id"])
        layer = _layer_of(c, p["lot_id"])
        after_totals[layer] = round(after_totals.get(layer, 0.0)
                                    + (p["after_qty_remain"] - p["before_qty_remain"]), 6)
    payload = {
        "consumption_id": cid,
        "item_id": row.get("item_id"),
        "reason": reason.strip(),
        "restores": projected,
        "totals": {layer: {"before": before_totals.get(layer, 0.0),
                           "after": after_totals.get(layer, 0.0)}
                   for layer in sorted(set(before_totals) | set(after_totals))},
        "committed": commit,
    }
    if commit:
        now = datetime.now(timezone.utc).isoformat()
        for p in projected:
            c.execute("UPDATE lots SET qty_remain=?, status=? WHERE id=?",
                      (p["after_qty_remain"], p["after_status"], p["lot_id"]))
        c.execute("UPDATE consumptions SET reversed_at=?, reason=? WHERE id=?", (now, reason.strip(), cid))
        c.execute(
            "INSERT INTO consumptions(note,result_json,created_at,kind,source_id,reason,item_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (row.get("note"), json.dumps({"restores": projected}), now,
             "reverse", cid, reason.strip(), row.get("item_id")))
    return payload

def _layer_of(c, lot_id: int) -> str:
    r = c.execute("SELECT items.layer AS layer FROM lots JOIN items ON items.id=lots.item_id WHERE lots.id=?",
                  (lot_id,)).fetchone()
    return r["layer"] if r else "unknown"

@app.post("/api/consumptions/{cid}/reverse/preview")
def reverse_preview(cid: int, body: ReverseIn):
    c = connect()
    c.execute("BEGIN IMMEDIATE")
    try:
        payload = _reverse_projection(c, cid, body.reason, commit=False)
    finally:
        c.rollback(); c.close()
    return payload

@app.post("/api/consumptions/{cid}/reverse")
def reverse_confirm(cid: int, body: ReverseIn):
    c = connect()
    c.execute("BEGIN IMMEDIATE")
    try:
        payload = _reverse_projection(c, cid, body.reason, commit=True)
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
    return payload

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows
