from app.engines.fefo import plan_restore, restored_status

TODAY = "2026-10-05"

def _lot(lid, qty, status="on_shelf", expiry="2026-12-01"):
    return {"id": lid, "qty_remain": qty, "status": status, "expiry": expiry}

def test_restore_untouched_future_lot():
    plan = plan_restore([{"lot_id": 1, "take": 2}], [_lot(1, 4)], TODAY)
    assert plan["ok"]
    r = plan["restorations"][0]
    assert (r["qty_before"], r["qty_after"], r["status_before"], r["status_after"]) == (4.0, 6.0, "on_shelf", "on_shelf")

def test_restore_drained_and_past_expiry_goes_expired():
    plan = plan_restore([{"lot_id": 1, "take": 1}],
                        [_lot(1, 0, status="consumed", expiry="2026-09-28")], TODAY)
    r = plan["restorations"][0]
    assert r["qty_after"] == 1.0 and r["status_after"] == "expired"

def test_restore_expired_is_sticky_even_for_future_expiry():
    # 异常数据：已下架却挂着未来到期 —— 冲正也绝不复活
    assert restored_status("expired", "2099-01-01", TODAY) == "expired"
    plan = plan_restore([{"lot_id": 1, "take": 2}],
                        [_lot(1, 3, status="expired", expiry="2099-01-01")], TODAY)
    assert plan["restorations"][0]["status_after"] == "expired"
    assert plan["restorations"][0]["qty_after"] == 5.0

def test_restore_expiry_boundary():
    assert restored_status("on_shelf", TODAY, TODAY) == "expired"
    assert restored_status("on_shelf", "2026-10-06", TODAY) == "on_shelf"

def test_restore_null_expiry_stays_on_shelf():
    plan = plan_restore([{"lot_id": 1, "take": 2}], [_lot(1, 0, status="consumed", expiry=None)], TODAY)
    assert plan["restorations"][0]["status_after"] == "on_shelf"

def test_restore_after_newer_consume_same_lot():
    # 原始 5，本笔扣 2（->3），之后新消费又扣 1（当前 2）；加回 2 得 4 = 新消费之前
    plan = plan_restore([{"lot_id": 1, "take": 2}], [_lot(1, 2)], TODAY)
    assert plan["restorations"][0]["qty_after"] == 4.0

def test_restore_missing_lot():
    plan = plan_restore([{"lot_id": 99, "take": 1}], [_lot(1, 2)], TODAY)
    assert not plan["ok"] and plan["reason"] == "lot_missing"

def test_restore_follows_deduction_order():
    lots = [_lot(2, 1, expiry="2026-11-01"), _lot(1, 1, expiry="2026-10-01")]
    plan = plan_restore([{"lot_id": 2, "take": 1}, {"lot_id": 1, "take": 1}], lots, TODAY)
    assert [r["lot_id"] for r in plan["restorations"]] == [2, 1]
