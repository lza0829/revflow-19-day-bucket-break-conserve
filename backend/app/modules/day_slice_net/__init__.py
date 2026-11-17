"""按入账日净贡献守恒重算（日切片净额）。

落库形态（两种择一，本仓选「账户+入账日贡献表整体替换」）：
  day_contributions（账户、入账日、净额分、重算运行 id …），按 (account_id, day) 整格替换；
  账户余额 = 该账户全部日贡献之和，日贡献合计与已归并流水净额同源守恒。
  （不采用「归并运行下按日不可变快照列」。）

守恒发布路径（两种择一，本仓选「自身入账日入桶 + 整体替换 + 整趟回滚」）：
  正向与冲正一律按流水自身 booked_at 的入账日入桶——冲正不得记入其目标正向的入账日
  （跨日串桶）；归并运行仅对本趟触及账户整体替换日贡献，并按账户同源写守恒证明
  （conserve_proofs：各日净贡献、全日合计、正冲净额、差额，挂归并运行 id，只读可查）；
  任一账户 Σ 日贡献 ≠ 已归并正冲净额（差额非零），整趟归并回滚并另记「失败」运行，
  未触及账户与邻日未触及桶禁止改写。
  （不采用「日贡献按归并运行 id 追加不可变快照、仅发布通过守恒的运行」。）

失败闭（同事务回滚）：
  日贡献写回、总余额重算、守恒校验全部在同一事务内完成；任一校验不过即整体回滚，
  日贡献与总余额保持重算前状态，另起事务登记「失败」运行备查。
  禁止出现「日贡献已新、总余额仍旧」或「邻日贡献被改」。

守恒不变式：
  该日正向合计 − 该日已配平（已归并）冲正合计 = 该日净贡献（均按自身入账日入桶）
  Σ 日贡献 = 账户已归并流水净额 = 账户余额
  邻日（本账户其他入账日）与未触及账户的贡献逐行不变
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect, rows_to_dicts

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DayRecalcFailed(Exception):
    """守恒校验失败：整次回滚，另记失败运行。"""

    def __init__(self, message: str, context: dict | None = None):
        super().__init__(message)
        self.context = context or {}


class SettleConserveFailed(Exception):
    """归并守恒证明差额非零：整趟回滚，另记失败运行。"""

    def __init__(self, message: str, context: dict | None = None):
        super().__init__(message)
        self.context = context or {}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- 同源派生

def _derive_day(conn, account_id: int, day: str) -> dict:
    """从流水管道派生某账户某入账日的 正向/已配平冲正 合计（双路同源）。

    入桶规则：正向与冲正均按【自身 booked_at】的入账日入桶；
    冲正不得串入其目标正向的入账日（即使跨日冲正，也落在冲正自己的入账日）。
    """
    rows = rows_to_dicts(
        conn.execute(
            """
            SELECT kind, amount_cents FROM ledger_entries
            WHERE account_id = ? AND status = '已归并'
              AND substr(booked_at, 1, 10) = ?
            """,
            (account_id, day),
        ).fetchall()
    )
    fwd_py = sum(r["amount_cents"] for r in rows if r["kind"] == "正向")
    rev_py = sum(r["amount_cents"] for r in rows if r["kind"] == "冲正")
    agg = conn.execute(
        """
        SELECT
          COALESCE(SUM(CASE WHEN kind = '正向' THEN amount_cents END), 0) AS f,
          COALESCE(SUM(CASE WHEN kind = '冲正' THEN amount_cents END), 0) AS r
        FROM ledger_entries
        WHERE account_id = ? AND status = '已归并'
          AND substr(booked_at, 1, 10) = ?
        """,
        (account_id, day),
    ).fetchone()
    return {
        "forward": fwd_py,
        "reversal": rev_py,
        "agg_forward": agg["f"],
        "agg_reversal": agg["r"],
        "entry_count": len(rows),
    }


def _ledger_net(conn, account_id: int) -> int:
    """账户全量已归并流水净额（正向 − 已配平冲正）。"""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN kind = '正向' THEN amount_cents ELSE -amount_cents END), 0) AS n
        FROM ledger_entries
        WHERE account_id = ? AND status = '已归并'
        """,
        (account_id,),
    ).fetchone()
    return row["n"]


def _contribution_rows(conn, account_id: int) -> list[dict]:
    return rows_to_dicts(
        conn.execute(
            "SELECT * FROM day_contributions WHERE account_id = ? ORDER BY day",
            (account_id,),
        ).fetchall()
    )


def _pending_count(conn, account_id: int, day: str) -> int:
    return conn.execute(
        """
        SELECT COUNT(*) AS c FROM ledger_entries
        WHERE account_id = ? AND status = '待归并' AND substr(booked_at, 1, 10) = ?
        """,
        (account_id, day),
    ).fetchone()["c"]


# ---------------------------------------------------------------- 全局归并挂钩

def rebuild_account_days(conn, account_id: int, *, settle_run_id: int, now: str) -> int:
    """全局净额归并后，按账户从已归并流水重建其全部日贡献，返回账户净额。

    正向与冲正均按流水自身 booked_at 的入账日入桶；只重写本账户的日贡献行
    （其他账户不动）；行 provenance 记为本次归并运行。
    """
    agg = rows_to_dicts(
        conn.execute(
            """
            SELECT substr(booked_at, 1, 10) AS day,
                   COALESCE(SUM(CASE WHEN kind = '正向' THEN amount_cents END), 0) AS f,
                   COALESCE(SUM(CASE WHEN kind = '冲正' THEN amount_cents END), 0) AS r
            FROM ledger_entries
            WHERE account_id = ? AND status = '已归并'
            GROUP BY substr(booked_at, 1, 10)
            """,
            (account_id,),
        ).fetchall()
    )
    conn.execute("DELETE FROM day_contributions WHERE account_id = ?", (account_id,))
    total = 0
    for r in agg:
        net = r["f"] - r["r"]
        conn.execute(
            """
            INSERT INTO day_contributions(
              account_id, day, net_cents, forward_cents, reversal_cents,
              settle_run_id, recalc_run_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (account_id, r["day"], net, r["f"], r["r"], settle_run_id, now),
        )
        total += net
    return total


def write_conserve_proof(conn, account_id: int, *, settle_run_id: int, now: str) -> dict:
    """为本趟归并触及的账户写守恒证明（挂归并运行 id）；差额非零即抛错，整趟回滚。

    同源派生：各日净贡献取自本趟刚落库的 day_contributions 行，
    正冲净额取自同一事务内的已归并流水——两侧同一来源，不得另起口径。
    证明行只在守恒通过时随本趟事务提交落库；回滚即不发布。
    """
    days = rows_to_dicts(
        conn.execute(
            """
            SELECT day, net_cents, forward_cents, reversal_cents
            FROM day_contributions WHERE account_id = ? ORDER BY day
            """,
            (account_id,),
        ).fetchall()
    )
    total = sum(r["net_cents"] for r in days)
    ledger_net = _ledger_net(conn, account_id)
    diff = total - ledger_net
    proof = {
        "account_id": account_id,
        "day_nets": days,
        "total_cents": total,
        "ledger_net_cents": ledger_net,
        "diff_cents": diff,
    }
    if diff != 0:
        raise SettleConserveFailed(
            f"账户 #{account_id} 全日合计 {total} 分 ≠ 已归并正冲净额 {ledger_net} 分"
            f"（差额 {diff} 分），不守恒",
            proof,
        )
    conn.execute(
        """
        INSERT INTO conserve_proofs(
          settle_run_id, account_id, day_nets_json, day_count,
          total_cents, ledger_net_cents, diff_cents, consistent, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 0, 1, ?)
        """,
        (
            settle_run_id,
            account_id,
            json.dumps(days, ensure_ascii=False),
            len(days),
            total,
            ledger_net,
            now,
        ),
    )
    return proof


# ---------------------------------------------------------------- 按日重算

def recalc_day(account_id: int, day: str) -> dict:
    """重算指定账户+入账日的日净贡献并写回；守恒不过则整次回滚。"""
    if not DAY_RE.match(day):
        raise HTTPException(400, "入账日格式须为 YYYY-MM-DD")

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = _now()

        acct = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if not acct:
            raise HTTPException(404, "账户不存在")

        d = _derive_day(conn, account_id, day)
        if (d["forward"], d["reversal"]) != (d["agg_forward"], d["agg_reversal"]):
            raise DayRecalcFailed(
                "流水双路派生不一致，拒绝写回",
                {"逐笔派生": [d["forward"], d["reversal"]], "聚合派生": [d["agg_forward"], d["agg_reversal"]]},
            )

        existing = conn.execute(
            "SELECT * FROM day_contributions WHERE account_id = ? AND day = ?",
            (account_id, day),
        ).fetchone()
        if d["entry_count"] == 0 and not existing:
            raise HTTPException(400, "该账户该入账日无已归并流水，无需重算")

        net = d["forward"] - d["reversal"]
        prev_net = existing["net_cents"] if existing else 0
        balance_before = acct["balance_cents"]
        neighbors_before = {
            r["day"]: r["net_cents"] for r in _contribution_rows(conn, account_id) if r["day"] != day
        }
        pending = _pending_count(conn, account_id, day)

        # 先登记运行行，以其 id 作为日贡献的「重算运行 id」provenance
        cur = conn.execute(
            """
            INSERT INTO day_recalc_runs(account_id, day, status, created_at)
            VALUES (?, ?, '进行中', ?)
            """,
            (account_id, day, now),
        )
        run_id = cur.lastrowid

        # 写回该日贡献：整格替换（无已归并流水则清零即删行）；邻日行不触碰
        if d["entry_count"] == 0:
            conn.execute(
                "DELETE FROM day_contributions WHERE account_id = ? AND day = ?",
                (account_id, day),
            )
        else:
            conn.execute(
                """
                INSERT INTO day_contributions(
                  account_id, day, net_cents, forward_cents, reversal_cents,
                  settle_run_id, recalc_run_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(account_id, day) DO UPDATE SET
                  net_cents = excluded.net_cents,
                  forward_cents = excluded.forward_cents,
                  reversal_cents = excluded.reversal_cents,
                  settle_run_id = NULL,
                  recalc_run_id = excluded.recalc_run_id,
                  updated_at = excluded.updated_at
                """,
                (account_id, day, net, d["forward"], d["reversal"], run_id, now),
            )

        # 守恒校验（同事务）：全日各日净贡献之和必须等于已归并正冲净额，不过则整次回滚
        contrib_total = conn.execute(
            "SELECT COALESCE(SUM(net_cents), 0) AS s FROM day_contributions WHERE account_id = ?",
            (account_id,),
        ).fetchone()["s"]
        ledger_net = _ledger_net(conn, account_id)
        if contrib_total != ledger_net:
            raise DayRecalcFailed(
                f"日贡献合计 {contrib_total} 分 ≠ 已归并正冲净额 {ledger_net} 分"
                f"（差额 {contrib_total - ledger_net} 分），不守恒",
                {"日贡献合计分": contrib_total, "流水净额分": ledger_net, "差额分": contrib_total - ledger_net},
            )

        # 同事务重算总余额：账户余额 = Σ 日贡献（禁止「日贡献已新、总余额仍旧」）
        conn.execute(
            "UPDATE accounts SET balance_cents = ? WHERE id = ?",
            (contrib_total, account_id),
        )
        balance_after = contrib_total

        # 邻日未触及桶禁止改写：逐行比对前后快照，被改即整次回滚
        neighbors_after = {
            r["day"]: r["net_cents"] for r in _contribution_rows(conn, account_id) if r["day"] != day
        }
        if neighbors_after != neighbors_before:
            raise DayRecalcFailed(
                "邻日贡献在重算中被改写，拒绝提交",
                {"邻日前": neighbors_before, "邻日后": neighbors_after},
            )

        report = {
            "账户": acct["code"],
            "账户名": acct["name"],
            "入账日": day,
            "日正向分": d["forward"],
            "日冲正分": d["reversal"],
            "日净贡献分": net,
            "重算前日净分": prev_net,
            "余额前分": balance_before,
            "余额后分": balance_after,
            "该日待归并笔数": pending,
            "邻日抽检": [
                {"入账日": k, "前": neighbors_before[k], "后": neighbors_after[k], "不变": True}
                for k in sorted(neighbors_before)
            ],
            "同源校验": {
                "日贡献合计分": contrib_total,
                "流水净额分": ledger_net,
                "账户余额分": balance_after,
                "守恒": contrib_total == ledger_net == balance_after,
            },
        }
        conn.execute(
            """
            UPDATE day_recalc_runs
            SET status = '完成', forward_cents = ?, reversal_cents = ?, net_cents = ?,
                prev_net_cents = ?, balance_before_cents = ?, balance_after_cents = ?,
                report_json = ?
            WHERE id = ?
            """,
            (
                d["forward"], d["reversal"], net, prev_net,
                balance_before, balance_after,
                json.dumps(report, ensure_ascii=False),
                run_id,
            ),
        )
        conn.commit()
        return get_run(run_id)
    except DayRecalcFailed as e:
        conn.rollback()
        fail_id = _record_failure(account_id, day, str(e), e.context)
        raise HTTPException(409, f"日切片守恒校验失败，已整次回滚（失败运行 #{fail_id}）：{e}")
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _record_failure(account_id: int, day: str, error: str, context: dict) -> int:
    """回滚后另起事务登记失败运行，备查不改账。"""
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO day_recalc_runs(account_id, day, status, error, report_json, created_at)
            VALUES (?, ?, '失败', ?, ?, ?)
            """,
            (account_id, day, error, json.dumps(context, ensure_ascii=False), _now()),
        )
        conn.commit()
        return cur.lastrowid


# ---------------------------------------------------------------- 查询

def _run_out(row: dict) -> dict:
    out = dict(row)
    try:
        out["report"] = json.loads(out.pop("report_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        out["report"] = {}
    return out


def list_runs() -> list[dict]:
    with connect() as conn:
        rows = rows_to_dicts(
            conn.execute(
                """
                SELECT r.*, a.code AS account_code, a.name AS account_name
                FROM day_recalc_runs r
                JOIN accounts a ON a.id = r.account_id
                ORDER BY r.id DESC
                """
            ).fetchall()
        )
        return [_run_out(r) for r in rows]


def get_run(run_id: int) -> dict:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT r.*, a.code AS account_code, a.name AS account_name
            FROM day_recalc_runs r
            JOIN accounts a ON a.id = r.account_id
            WHERE r.id = ?
            """,
            (run_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "重算运行不存在")
        return _run_out(dict(row))


def list_contributions(account_id: int | None = None) -> list[dict]:
    with connect() as conn:
        sql = """
            SELECT c.*, a.code AS account_code, a.name AS account_name
            FROM day_contributions c
            JOIN accounts a ON a.id = c.account_id
        """
        params: tuple = ()
        if account_id is not None:
            sql += " WHERE c.account_id = ?"
            params = (account_id,)
        sql += " ORDER BY c.account_id, c.day"
        return rows_to_dicts(conn.execute(sql, params).fetchall())


def list_conserve_proofs(settle_run_id: int) -> dict:
    """按归并运行 id 只读查询守恒证明（各日净贡献、全日合计、正冲净额、差额）。

    证明行来自 conserve_proofs 实体（归并发布时同源派生落库），此处只读不改写。
    """
    with connect() as conn:
        run = conn.execute("SELECT * FROM settle_runs WHERE id = ?", (settle_run_id,)).fetchone()
        if not run:
            raise HTTPException(404, "归并运行不存在")
        rows = rows_to_dicts(
            conn.execute(
                """
                SELECT p.*, a.code AS account_code, a.name AS account_name
                FROM conserve_proofs p
                JOIN accounts a ON a.id = p.account_id
                WHERE p.settle_run_id = ?
                ORDER BY p.account_id
                """,
                (settle_run_id,),
            ).fetchall()
        )
        proofs = []
        for r in rows:
            try:
                r["day_nets"] = json.loads(r.pop("day_nets_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                r["day_nets"] = []
            proofs.append(r)
        return {
            "settle_run_id": settle_run_id,
            "run_status": run["status"],
            "proof_count": len(proofs),
            "total_cents": sum(p["total_cents"] for p in proofs),
            "ledger_net_cents": sum(p["ledger_net_cents"] for p in proofs),
            "diff_cents": sum(p["diff_cents"] for p in proofs),
            "consistent": 1 if all(p["consistent"] == 1 for p in proofs) else 0,
            "proofs": proofs,
        }
