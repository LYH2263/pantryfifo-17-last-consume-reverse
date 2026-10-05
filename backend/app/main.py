import json
from contextlib import contextmanager
from datetime import date, datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.engines.fefo import consume_fefo, expire_lots, plan_restore

app = FastAPI(title="Pantryfifo", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()


@contextmanager
def write_tx():
    """写事务：立刻取 RESERVED 锁，consume / sweep / reverse 三类写者串行化。

    所有"判定能否写"的 SELECT 都必须发生在事务内，以锁内重读为准。
    """
    c = connect()
    c.isolation_level = None  # 切到 autocommit 模式，BEGIN/COMMIT 由我们显式控制
    try:
        c.execute("BEGIN IMMEDIATE")
        yield c
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    finally:
        c.close()


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
    if body.qty <= 0:
        raise HTTPException(400, "qty_non_positive")
    with write_tx() as c:
        lots = [dict(r) for r in c.execute(
            "SELECT * FROM lots WHERE item_id=? AND status='on_shelf' AND qty_remain>0", (body.item_id,))]
        result = consume_fefo(lots, body.qty)
        if not result["ok"]:
            raise HTTPException(409, result)
        for d in result["deductions"]:
            c.execute("UPDATE lots SET qty_remain = qty_remain - ? WHERE id=?", (d["take"], d["lot_id"]))
            rem = c.execute("SELECT qty_remain FROM lots WHERE id=?", (d["lot_id"],)).fetchone()["qty_remain"]
            if rem <= 0:
                c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?", (d["lot_id"],))
        c.execute("""INSERT INTO consumptions(kind,item_id,note,result_json,created_at)
                     VALUES ('consume',?,?,?,?)""",
                  (body.item_id, body.note, json.dumps(result), datetime.now(timezone.utc).isoformat()))
    return result

@app.post("/api/expire-sweep")
def expire_sweep():
    with write_tx() as c:
        lots = [dict(r) for r in c.execute("SELECT * FROM lots WHERE status='on_shelf'")]
        ids = expire_lots(lots, date.today().isoformat())
        for i in ids:
            c.execute("UPDATE lots SET status='expired' WHERE id=?", (i,))
    return {"expired_ids": ids}

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows


# ---------------- 冲正（reverse latest consumption） ----------------

def _safe_json(text: str | None) -> dict:
    try:
        return json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}

def _latest_reversible_id(c) -> int | None:
    """最近一笔可冲的成功消费：consume 行、未冲正、id 最大。reverse 行永不参与。"""
    row = c.execute("""
        SELECT id FROM consumptions
        WHERE (kind='consume' OR kind IS NULL) AND reversed_at IS NULL
        ORDER BY id DESC LIMIT 1""").fetchone()
    return row["id"] if row else None

def _get_target(c, consumption_id: int | None):
    """返回 (row, error)；error ∈ None / no_consumption / unknown_consumption / not_a_consumption。"""
    if consumption_id is None:
        cid = _latest_reversible_id(c)
        if cid is None:
            return None, "no_consumption"
        return c.execute("SELECT * FROM consumptions WHERE id=?", (cid,)).fetchone(), None
    row = c.execute("SELECT * FROM consumptions WHERE id=?", (consumption_id,)).fetchone()
    if row is None:
        return None, "unknown_consumption"
    if row["kind"] not in ("consume", None):
        return None, "not_a_consumption"
    return row, None

def _target_error(row, c) -> str | None:
    """锁内/即时重算：已冲正 or 已不是最近一笔。"""
    if row["reversed_at"] is not None:
        return "already_reversed"
    if row["id"] != _latest_reversible_id(c):
        return "not_latest"
    return None

def _build_plan(c, row):
    """解析目标笔 deductions，按批次*当前*快照生成冲正加回计划。"""
    payload = _safe_json(row["result_json"])
    deductions = payload.get("deductions") or []
    if not deductions:
        return [], {"ok": False, "reason": "no_deductions", "restorations": []}
    qmarks = ",".join("?" for _ in deductions)
    lots = [dict(r) for r in c.execute(
        f"SELECT id,qty_remain,status,expiry FROM lots WHERE id IN ({qmarks})",
        [d["lot_id"] for d in deductions])]
    return deductions, plan_restore(deductions, lots, date.today().isoformat())

def _item_name(c, item_id):
    if item_id is None:
        return None
    r = c.execute("SELECT name FROM items WHERE id=?", (item_id,)).fetchone()
    return r["name"] if r else "（已删除商品）"

def _target_dict(c, row) -> dict:
    payload = _safe_json(row["result_json"])
    deductions = payload.get("deductions") or []
    item_id = row["item_id"]
    if item_id is None and deductions:
        lr = c.execute("SELECT item_id FROM lots WHERE id=?", (deductions[0]["lot_id"],)).fetchone()
        item_id = lr["item_id"] if lr else None
    return {
        "id": row["id"], "item_id": item_id, "item_name": _item_name(c, item_id),
        "note": row["note"], "created_at": row["created_at"], "deductions": deductions,
    }

@app.get("/api/consumptions")
def consumptions():
    """履历倒序：消费笔 + 冲正标记笔。全链至多一条 reversible=true。"""
    c = connect()
    rows = [dict(r) for r in c.execute("SELECT * FROM consumptions ORDER BY id DESC")]
    latest_id = _latest_reversible_id(c)
    item_names = {r["id"]: r["name"] for r in c.execute("SELECT id,name FROM items")}
    lot_items = {r["id"]: r["item_id"] for r in c.execute("SELECT id,item_id FROM lots")}
    rev_of = {r["reverses_id"]: r["id"] for r in rows if r["reverses_id"] is not None}
    c.close()
    out = []
    for r in rows:
        kind = r.get("kind") or "consume"
        payload = _safe_json(r.get("result_json"))
        deductions = payload.get("deductions") or []
        item_id = r.get("item_id")
        if item_id is None and deductions:
            item_id = lot_items.get(deductions[0]["lot_id"])
        item = {
            "id": r["id"], "kind": kind, "item_id": item_id,
            "item_name": item_names.get(item_id) if item_id is not None else None,
            "note": r.get("note") or "", "created_at": r.get("created_at"),
            "deductions": deductions,
            "reversed_at": r.get("reversed_at"),
            "reverse_reason": r.get("reverse_reason"),
            "reverses_id": r.get("reverses_id"),
            "reversal_id": rev_of.get(r["id"]),
        }
        if not item["item_name"] and item_id is not None:
            item["item_name"] = "（已删除商品）"
        if kind == "reverse":
            item["restorations"] = payload.get("restorations") or []
        item["reversible"] = (
            kind == "consume" and r.get("reversed_at") is None and r["id"] == latest_id
        )
        out.append(item)
    return out

class ReverseIn(BaseModel):
    consumption_id: int | None = None
    reason: str = ""

@app.post("/api/reverse/preview")
def reverse_preview(body: ReverseIn):
    """只读预览：绝不写库、全层余量不变。业务不可冲返回 200+valid:false。"""
    c = connect()
    try:
        row, err = _get_target(c, body.consumption_id)
        if err == "unknown_consumption":
            raise HTTPException(404, "unknown_consumption")
        if err == "not_a_consumption":
            raise HTTPException(409, "not_a_consumption")
        latest = _latest_reversible_id(c)
        if err == "no_consumption":
            return {"target": None, "valid": False, "error": "no_consumption",
                    "latest_reversible_id": None, "restorations": []}
        target = _target_dict(c, row)
        terr = _target_error(row, c)
        if terr:
            return {"target": target, "valid": False, "error": terr,
                    "latest_reversible_id": latest, "restorations": []}
        _, plan = _build_plan(c, row)
        if not plan["ok"]:
            return {"target": target, "valid": False, "error": plan["reason"],
                    "latest_reversible_id": latest, "restorations": []}
        return {"target": target, "valid": True, "error": None,
                "latest_reversible_id": latest, "restorations": plan["restorations"]}
    finally:
        c.close()

@app.post("/api/reverse")
def reverse(body: ReverseIn):
    """确认冲正：原因字必填；锁内重算最近一笔/未冲正；逐批加回并重判 status。"""
    if not body.reason or not body.reason.strip():
        raise HTTPException(400, "reason_required")
    reason = body.reason.strip()
    with write_tx() as c:
        row, err = _get_target(c, body.consumption_id)
        if err == "unknown_consumption":
            raise HTTPException(404, "unknown_consumption")
        if err in ("not_a_consumption", "no_consumption"):
            raise HTTPException(409, err)
        terr = _target_error(row, c)
        if terr:
            raise HTTPException(409, terr)
        deductions, plan = _build_plan(c, row)
        if not plan["ok"]:
            raise HTTPException(409, plan["reason"])
        restorations = plan["restorations"]
        target_id, item_id = row["id"], row["item_id"]
        now = datetime.now(timezone.utc).isoformat()
        for rst in restorations:
            c.execute("UPDATE lots SET qty_remain=?, status=? WHERE id=?",
                      (rst["qty_after"], rst["status_after"], rst["lot_id"]))
        c.execute("UPDATE consumptions SET reversed_at=?, reverse_reason=? WHERE id=?",
                  (now, reason, target_id))
        cur = c.execute("""INSERT INTO consumptions(kind,item_id,reverses_id,note,result_json,created_at)
                           VALUES ('reverse',?,?,?,?,?)""",
                        (item_id, target_id, reason,
                         json.dumps({"ok": True, "deductions": deductions, "restorations": restorations}),
                         now))
        reversal_id = cur.lastrowid
    return {"ok": True, "reversed_id": target_id, "reversal_id": reversal_id,
            "restorations": restorations}
