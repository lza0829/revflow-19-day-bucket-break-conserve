"""日切片净额：按入账日重算净贡献的守恒与失败闭测试。

夹具账目（全部归并后）：
  账户1：2026-11-15 净 11000（15000-4000）
        2026-11-16 净 28000（43000-15000）
        2026-11-17 净 17000（20000-3000）  余额 56000
  账户2：2026-11-16 净 0（18000-18000）
        2026-11-17 净 7000（9000-2000）   余额 7000
  账户3：2026-11-16 净 50000             余额 50000
  REV-20261116-004 无原单 → 隔离，不计入任何日贡献
"""

from pathlib import Path
import os
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
os.environ["DATA_DIR"] = str(ROOT / "backend" / "data_test")
os.environ["FIXTURES_DIR"] = str(ROOT / "fixtures")

db_path = Path(os.environ["DATA_DIR"]) / "app.db"

from app.main import app  # noqa: E402
from app.db import connect  # noqa: E402


@pytest.fixture(scope="module")
def client():
    if db_path.exists():
        db_path.unlink()
    with TestClient(app) as c:
        yield c


def _contributions(account_id):
    with connect() as conn:
        return {
            r["day"]: dict(r)
            for r in conn.execute(
                "SELECT * FROM day_contributions WHERE account_id = ?", (account_id,)
            ).fetchall()
        }


def _balance(account_id):
    with connect() as conn:
        return conn.execute(
            "SELECT balance_cents AS b FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()["b"]


def _corrupt_contribution(account_id, day, new_net):
    with connect() as conn:
        conn.execute(
            "UPDATE day_contributions SET net_cents = ? WHERE account_id = ? AND day = ?",
            (new_net, account_id, day),
        )
        conn.commit()


def test_settle_materializes_day_contributions(client):
    r = client.post("/api/settle-runs")
    assert r.status_code == 200, r.text

    rows = client.get("/api/day-contributions").json()
    got = {(c["account_id"], c["day"]): c["net_cents"] for c in rows}
    assert got == {
        (1, "2026-11-15"): 11000,
        (1, "2026-11-16"): 28000,
        (1, "2026-11-17"): 17000,
        (2, "2026-11-16"): 0,
        (2, "2026-11-17"): 7000,
        (3, "2026-11-16"): 50000,
    }
    # 每行都带归并 provenance；余额 = Σ 日贡献
    assert all(c["settle_run_id"] for c in rows)
    for acc_id, expect in [(1, 56000), (2, 7000), (3, 50000)]:
        mine = [c["net_cents"] for c in rows if c["account_id"] == acc_id]
        assert sum(mine) == expect == _balance(acc_id)


def test_recalc_day_conserves_and_neighbors_untouched(client):
    before = _contributions(1)
    r = client.post("/api/day-recalc-runs", json={"account_id": 1, "day": "2026-11-16"})
    assert r.status_code == 200, r.text
    body = r.json()
    rep = body["report"]

    # 报告数字与流水同源：43000 - 15000 = 28000
    assert rep["日正向分"] == 43000
    assert rep["日冲正分"] == 15000
    assert rep["日净贡献分"] == 28000
    assert rep["重算前日净分"] == 28000
    assert rep["余额前分"] == rep["余额后分"] == 56000
    assert rep["同源校验"] == {
        "日贡献合计分": 56000,
        "流水净额分": 56000,
        "账户余额分": 56000,
        "守恒": True,
    }
    # 邻日抽检：前后两日都在且不变
    assert {n["入账日"]: n["不变"] for n in rep["邻日抽检"]} == {
        "2026-11-15": True,
        "2026-11-17": True,
    }

    after = _contributions(1)
    # 邻日行逐字段不变（含归并 provenance），仅目标日改挂本次重算运行
    for day in ("2026-11-15", "2026-11-17"):
        assert after[day] == before[day]
    assert after["2026-11-16"]["net_cents"] == 28000
    assert after["2026-11-16"]["recalc_run_id"] == body["id"]
    assert after["2026-11-16"]["settle_run_id"] is None
    assert _balance(1) == 56000


def test_recalc_heals_target_day_drift(client):
    _corrupt_contribution(1, "2026-11-16", 99999)
    r = client.post("/api/day-recalc-runs", json={"account_id": 1, "day": "2026-11-16"})
    assert r.status_code == 200, r.text
    assert r.json()["report"]["重算前日净分"] == 99999
    assert _contributions(1)["2026-11-16"]["net_cents"] == 28000
    assert _balance(1) == 56000


def test_recalc_heals_balance_drift(client):
    with connect() as conn:
        conn.execute("UPDATE accounts SET balance_cents = 1 WHERE id = 1")
        conn.commit()
    r = client.post("/api/day-recalc-runs", json={"account_id": 1, "day": "2026-11-17"})
    assert r.status_code == 200, r.text
    rep = r.json()["report"]
    assert rep["余额前分"] == 1
    assert rep["余额后分"] == 56000
    assert _balance(1) == 56000


def test_recalc_fails_closed_on_neighbor_drift(client):
    # 邻日贡献被改 → Σ 日贡献 ≠ 流水净额 → 整次失败，一切回到重算前
    _corrupt_contribution(1, "2026-11-15", 11500)
    before = _contributions(1)
    bal_before = _balance(1)

    r = client.post("/api/day-recalc-runs", json={"account_id": 1, "day": "2026-11-16"})
    assert r.status_code == 409, r.text
    assert "不守恒" in r.text or "守恒" in r.text

    # 日贡献（含被改的邻日）与总余额全部保持重算前，禁止「日贡献已新、余额仍旧」
    assert _contributions(1) == before
    assert _balance(1) == bal_before

    # 失败运行已登记备查
    runs = client.get("/api/day-recalc-runs").json()
    failed = [x for x in runs if x["status"] == "失败"]
    assert failed and "不守恒" in failed[0]["error"]

    # 清理：恢复邻日，保持后续用例环境干净
    _corrupt_contribution(1, "2026-11-15", 11000)


def test_recalc_validation(client):
    assert client.post(
        "/api/day-recalc-runs", json={"account_id": 999, "day": "2026-11-16"}
    ).status_code == 404
    assert client.post(
        "/api/day-recalc-runs", json={"account_id": 1, "day": "2026/11/16"}
    ).status_code == 400
    # 账户3 在 2026-11-17 无已归并流水
    assert client.post(
        "/api/day-recalc-runs", json={"account_id": 3, "day": "2026-11-17"}
    ).status_code == 400


def test_verify_still_consistent_after_recalc(client):
    r = client.post("/api/verify-reports")
    assert r.status_code == 200, r.text
    assert r.json()["consistent"] == 1
