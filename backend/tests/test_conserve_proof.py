"""守恒证明实体与跨日入桶回归测试。

覆盖：
  1. 归并运行为本趟触及账户发布守恒证明（各日净贡献、全日合计、正冲净额、差额），
     挂归并运行 id，可按运行 id 只读查询；
  2. 跨日冲正必须按自身 booked_at 入账日入桶（归并与按日重算一致），
     不得串入目标正向的入账日；
  3. 未触及账户的日桶在后续归并中逐行不变。
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


def test_settle_publishes_conserve_proofs(client):
    r = client.post("/api/settle-runs")
    assert r.status_code == 200, r.text
    run_id = r.json()["id"]

    p = client.get(f"/api/settle-runs/{run_id}/conserve-proofs")
    assert p.status_code == 200, p.text
    body = p.json()
    assert body["settle_run_id"] == run_id
    assert body["run_status"] == "完成"

    proofs = {x["account_id"]: x for x in body["proofs"]}
    assert set(proofs) == {1, 2, 3}

    # 各日净贡献与流水同源：账户1 三日净额
    assert {d["day"]: d["net_cents"] for d in proofs[1]["day_nets"]} == {
        "2026-11-15": 11000,
        "2026-11-16": 28000,
        "2026-11-17": 17000,
    }
    # 全日合计 == 正冲净额，差额为 0；各日净贡献之和 == 全日合计
    for acc_id, expect in [(1, 56000), (2, 7000), (3, 50000)]:
        pr = proofs[acc_id]
        assert pr["settle_run_id"] == run_id
        assert pr["total_cents"] == expect == pr["ledger_net_cents"]
        assert pr["diff_cents"] == 0
        assert pr["consistent"] == 1
        assert sum(d["net_cents"] for d in pr["day_nets"]) == pr["total_cents"]
        assert pr["day_count"] == len(pr["day_nets"])

    # 运行级合计：56000 + 7000 + 50000
    assert body["total_cents"] == 113000 == body["ledger_net_cents"]
    assert body["diff_cents"] == 0
    assert body["consistent"] == 1


def test_conserve_proofs_unknown_run_404(client):
    assert client.get("/api/settle-runs/99999/conserve-proofs").status_code == 404


def test_cross_day_reversal_buckets_by_own_booked_at(client):
    # 跨日冲正：正向 11-18 入账、其冲正 11-19 入账
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO ledger_entries(entry_key, account_id, kind, amount_cents, biz_ref,
                                       target_entry_key, booked_at, status)
            VALUES ('FWD-XDAY-001', 3, '正向', 20000, 'ORD-X1', NULL,
                    '2026-11-18T09:00:00Z', '待归并')
            """
        )
        conn.execute(
            """
            INSERT INTO ledger_entries(entry_key, account_id, kind, amount_cents, biz_ref,
                                       target_entry_key, booked_at, status)
            VALUES ('REV-XDAY-001', 3, '冲正', 5000, 'ORD-X1', 'FWD-XDAY-001',
                    '2026-11-19T09:00:00Z', '待归并')
            """
        )
        conn.commit()

    before = client.get("/api/day-contributions").json()
    r = client.post("/api/settle-runs")
    assert r.status_code == 200, r.text
    run_id = r.json()["id"]
    after = client.get("/api/day-contributions").json()

    got = {
        (c["account_id"], c["day"]): (c["forward_cents"], c["reversal_cents"], c["net_cents"])
        for c in after
    }
    # 冲正进自身入账日 11-19 日桶（净 -5000），不串入目标正向的 11-18
    assert got[(3, "2026-11-18")] == (20000, 0, 20000)
    assert got[(3, "2026-11-19")] == (0, 5000, -5000)
    # 账户3 余额 = Σ 日贡献 = 50000 + 20000 - 5000
    acc3 = client.get("/api/accounts").json()
    assert {a["id"]: a["balance_cents"] for a in acc3}[3] == 65000

    # 未触及账户（1、2）的日桶逐行不变（含 provenance）
    for acc_id in (1, 2):
        assert [c for c in after if c["account_id"] == acc_id] == [
            c for c in before if c["account_id"] == acc_id
        ]

    # 本趟守恒证明只覆盖触及账户（账户3）：全日合计 65000 = 正冲净额，差额 0
    body = client.get(f"/api/settle-runs/{run_id}/conserve-proofs").json()
    assert [x["account_id"] for x in body["proofs"]] == [3]
    pr = body["proofs"][0]
    assert pr["total_cents"] == 65000 == pr["ledger_net_cents"]
    assert pr["diff_cents"] == 0
    assert {d["day"]: d["net_cents"] for d in pr["day_nets"]} == {
        "2026-11-16": 50000,
        "2026-11-18": 20000,
        "2026-11-19": -5000,
    }

    # 按日重算同样按自身入账日取数：11-19 冲正 5000、净 -5000；11-18 无冲正
    r19 = client.post("/api/day-recalc-runs", json={"account_id": 3, "day": "2026-11-19"})
    assert r19.status_code == 200, r19.text
    rep19 = r19.json()["report"]
    assert rep19["日正向分"] == 0
    assert rep19["日冲正分"] == 5000
    assert rep19["日净贡献分"] == -5000
    assert rep19["同源校验"] == {
        "日贡献合计分": 65000,
        "流水净额分": 65000,
        "账户余额分": 65000,
        "守恒": True,
    }

    r18 = client.post("/api/day-recalc-runs", json={"account_id": 3, "day": "2026-11-18"})
    assert r18.status_code == 200, r18.text
    rep18 = r18.json()["report"]
    assert rep18["日正向分"] == 20000
    assert rep18["日冲正分"] == 0
    assert rep18["日净贡献分"] == 20000

    # 全局仍守恒：已归并流水净额 == 账户余额合计
    v = client.post("/api/verify-reports")
    assert v.status_code == 200, v.text
    assert v.json()["consistent"] == 1
