"""FEFO consume: earliest expiry first among positive remaining lots."""

def sort_lots_fefo(lots: list[dict]) -> list[dict]:
    return sorted(
        [l for l in lots if float(l.get("qty_remain", 0)) > 0],
        key=lambda l: (l.get("expiry") or "9999-99-99", l.get("id") or 0),
    )

def consume_fefo(lots: list[dict], qty: float) -> dict:
    """Return deductions list and leftover demand. Mutates copies only."""
    need = float(qty)
    if need <= 0:
        return {"ok": False, "reason": "qty_non_positive", "deductions": [], "short": 0.0}
    ordered = sort_lots_fefo(lots)
    deductions = []
    for lot in ordered:
        if need <= 0:
            break
        avail = float(lot["qty_remain"])
        take = min(avail, need)
        deductions.append({"lot_id": lot["id"], "take": take, "expiry": lot.get("expiry")})
        need -= take
    if need > 1e-9:
        return {"ok": False, "reason": "short", "deductions": deductions, "short": round(need, 3)}
    return {"ok": True, "reason": "", "deductions": deductions, "short": 0.0}

def expire_lots(lots: list[dict], today: str) -> list[int]:
    """Ids that should leave shelf: remaining>0 and expiry < today."""
    out = []
    for l in lots:
        exp = l.get("expiry")
        if exp and exp < today and float(l.get("qty_remain", 0)) > 0:
            out.append(l["id"])
    return out

FAR = "9999-12-31"

def restored_status(current: str, expiry: str | None, today: str) -> str:
    """A lot after qty is added back by a reversal.

    - already swept off shelf as expired stays expired (sticky expiry: the lot
      genuinely passed its date, it must never come back on_shelf);
    - otherwise (on_shelf, or consumed because THIS deduction emptied it) the
      lot is re-judged by expiry date: expired if its date has passed, else on_shelf.
    """
    if current == "expired":
        return "expired"
    if expiry and expiry < today:
        return "expired"
    return "on_shelf"

def project_restores(rows: list[dict], restores: list[dict], today: str) -> list[dict]:
    """Pure projection: apply reversal add-backs to current lot rows.

    rows: current lots (id, qty_remain, status, expiry ...). Same projection is
    used by preview and by commit, so "preview never mutates" still shows exactly
    what confirm would write. Returns one entry per touched lot.
    """
    by_id = {r["id"]: r for r in rows}
    out = []
    for d in restores:
        lot = by_id.get(d["lot_id"])
        if lot is None:
            continue
        before_qty = float(lot["qty_remain"])
        before_status = lot["status"]
        after_qty = round(before_qty + float(d["take"]), 6)
        # A fully consumed lot has qty_remain 0; expiry-date check uses its expiry.
        after_status = restored_status(before_status, lot.get("expiry"), today)
        if after_status in ("expired", "consumed"):
            after_qty = max(after_qty, 0.0)
        out.append({
            "lot_id": d["lot_id"],
            "expiry": lot.get("expiry"),
            "add_back": round(float(d["take"]), 3),
            "before_status": before_status,
            "after_status": after_status,
            "before_qty_remain": round(before_qty, 3),
            "after_qty_remain": round(after_qty, 3),
        })
    return out
