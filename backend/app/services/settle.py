from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect, rows_to_dicts
from app.modules.day_slice_net import rebuild_account_days


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dashboard() -> dict:
    with connect() as conn:
        accounts = rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY id").fetchall())
        fwd = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind = '正向'"
        ).fetchone()["c"]
        rev = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind = '冲正'"
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE status = '待归并'"
        ).fetchone()["c"]
        q = conn.execute("SELECT COUNT(*) AS c FROM quarantine_items").fetchone()["c"]
        bal = conn.execute("SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts").fetchone()["s"]
        last = conn.execute("SELECT * FROM settle_runs ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "账户数": len(accounts),
            "正向笔数": fwd,
            "冲正笔数": rev,
            "待归并": pending,
            "未平隔离": q,
            "余额合计分": bal,
            "最近归并": dict(last) if last else None,
            "账户": accounts,
        }


def list_accounts() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY id").fetchall())


def list_entries(kind: str | None = None) -> list[dict]:
    with connect() as conn:
        if kind:
            rows = conn.execute(
                """
                SELECT e.*, a.code AS account_code, a.name AS account_name
                FROM ledger_entries e
                JOIN accounts a ON a.id = e.account_id
                WHERE e.kind = ?
                ORDER BY e.id
                """,
                (kind,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT e.*, a.code AS account_code, a.name AS account_name
                FROM ledger_entries e
                JOIN accounts a ON a.id = e.account_id
                ORDER BY e.id
                """
            ).fetchall()
        return rows_to_dicts(rows)


def list_quarantine() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT q.*, a.code AS account_code
            FROM quarantine_items q
            JOIN accounts a ON a.id = q.account_id
            ORDER BY q.id DESC
            """
        ).fetchall()
        return rows_to_dicts(rows)


def list_settle_runs() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(
            conn.execute("SELECT * FROM settle_runs ORDER BY id DESC").fetchall()
        )


def list_verify_reports() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(
            conn.execute("SELECT * FROM verify_reports ORDER BY id DESC").fetchall()
        )


def run_settle() -> dict:
    """净额归并：待归并正向−已匹配冲正配平；无原单/超额冲正进隔离；
    随后按账户从已归并流水全量重建日贡献（物化分日净额），余额 = Σ 日贡献。"""
    with connect() as conn:
        now = _now()
        cur = conn.execute(
            """
            INSERT INTO settle_runs(status, started_at)
            VALUES ('进行中', ?)
            """,
            (now,),
        )
        run_id = cur.lastrowid

        forwards = rows_to_dicts(
            conn.execute(
                "SELECT * FROM ledger_entries WHERE kind = '正向' AND status = '待归并' ORDER BY id"
            ).fetchall()
        )
        reversals = rows_to_dicts(
            conn.execute(
                "SELECT * FROM ledger_entries WHERE kind = '冲正' AND status = '待归并' ORDER BY id"
            ).fetchall()
        )
        fwd_by_key = {f["entry_key"]: f for f in forwards}

        # 每个正向已占用冲正额度
        used: dict[str, int] = {f["entry_key"]: 0 for f in forwards}
        settled = 0
        quarantined = 0

        for r in reversals:
            target = r.get("target_entry_key")
            if not target or target not in fwd_by_key:
                conn.execute(
                    """
                    INSERT INTO quarantine_items(
                      entry_key, account_id, reason, amount_cents, settle_run_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (r["entry_key"], r["account_id"], "冲正无原单", r["amount_cents"], run_id, now),
                )
                conn.execute(
                    "UPDATE ledger_entries SET status = '已隔离', settle_run_id = ? WHERE id = ?",
                    (run_id, r["id"]),
                )
                quarantined += 1
                continue

            fwd = fwd_by_key[target]
            if fwd["account_id"] != r["account_id"]:
                conn.execute(
                    """
                    INSERT INTO quarantine_items(
                      entry_key, account_id, reason, amount_cents, settle_run_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (r["entry_key"], r["account_id"], "冲正账户与原单不一致", r["amount_cents"], run_id, now),
                )
                conn.execute(
                    "UPDATE ledger_entries SET status = '已隔离', settle_run_id = ? WHERE id = ?",
                    (run_id, r["id"]),
                )
                quarantined += 1
                continue

            remain = fwd["amount_cents"] - used[target]
            if r["amount_cents"] > remain:
                conn.execute(
                    """
                    INSERT INTO quarantine_items(
                      entry_key, account_id, reason, amount_cents, settle_run_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (r["entry_key"], r["account_id"], "冲正超额", r["amount_cents"], run_id, now),
                )
                conn.execute(
                    "UPDATE ledger_entries SET status = '已隔离', settle_run_id = ? WHERE id = ?",
                    (run_id, r["id"]),
                )
                quarantined += 1
                continue

            used[target] += r["amount_cents"]
            conn.execute(
                "UPDATE ledger_entries SET status = '已归并', settle_run_id = ? WHERE id = ?",
                (run_id, r["id"]),
            )
            settled += 1

        for f in forwards:
            conn.execute(
                "UPDATE ledger_entries SET status = '已归并', settle_run_id = ? WHERE id = ?",
                (run_id, f["id"]),
            )
            settled += 1

        # 全量重建：凡有已归并流水的账户，日贡献整表重衍、余额 = Σ 日贡献（自愈任何漂移）
        net_total = 0
        acc_ids = [
            r["account_id"]
            for r in conn.execute(
                "SELECT DISTINCT account_id FROM ledger_entries WHERE status = '已归并'"
            ).fetchall()
        ]
        for acc_id in acc_ids:
            acct_total = rebuild_account_days(conn, acc_id, settle_run_id=run_id, now=now)
            conn.execute(
                "UPDATE accounts SET balance_cents = ?, last_settle_run_id = ? WHERE id = ?",
                (acct_total, run_id, acc_id),
            )
            net_total += acct_total

        finished = _now()
        conn.execute(
            """
            UPDATE settle_runs
            SET status = '完成',
                forward_count = ?,
                reversal_count = ?,
                settled_count = ?,
                quarantine_count = ?,
                net_cents = ?,
                finished_at = ?
            WHERE id = ?
            """,
            (
                len(forwards),
                len(reversals),
                settled,
                quarantined,
                net_total,
                finished,
                run_id,
            ),
        )
        conn.commit()
        return get_settle_run(run_id)


def get_settle_run(run_id: int) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM settle_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "归并运行不存在")
        return dict(row)


def run_verify(settle_run_id: int | None = None) -> dict:
    with connect() as conn:
        if settle_run_id is None:
            last = conn.execute("SELECT id FROM settle_runs ORDER BY id DESC LIMIT 1").fetchone()
            if not last:
                raise HTTPException(400, "尚无归并运行，无法互证")
            settle_run_id = last["id"]
        run = conn.execute("SELECT * FROM settle_runs WHERE id = ?", (settle_run_id,)).fetchone()
        if not run:
            raise HTTPException(404, "归并运行不存在")

        # 已归并正向 − 已归并冲正
        fwd_sum = conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries
            WHERE kind = '正向' AND status = '已归并' AND settle_run_id = ?
            """,
            (settle_run_id,),
        ).fetchone()["s"]
        rev_sum = conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries
            WHERE kind = '冲正' AND status = '已归并' AND settle_run_id = ?
            """,
            (settle_run_id,),
        ).fetchone()["s"]
        ledger_net = fwd_sum - rev_sum
        balance_sum = conn.execute(
            "SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts WHERE last_settle_run_id = ?",
            (settle_run_id,),
        ).fetchone()["s"]
        # 账户余额合计可能含历史；底座约定：互证看「本轮触及账户余额合计」vs 本轮流水净额
        # 简化：用全部账户余额 vs 全部已归并净额（多轮后仍应一致若每轮覆盖写）
        all_bal = conn.execute("SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts").fetchone()["s"]
        q_count = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine_items WHERE settle_run_id = ?",
            (settle_run_id,),
        ).fetchone()["c"]

        # 全量已归并净额
        all_fwd = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries WHERE kind = '正向' AND status = '已归并'"
        ).fetchone()["s"]
        all_rev = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries WHERE kind = '冲正' AND status = '已归并'"
        ).fetchone()["s"]
        all_ledger_net = all_fwd - all_rev

        consistent = 1 if all_ledger_net == all_bal else 0
        detail = {
            "本轮正向分": fwd_sum,
            "本轮冲正分": rev_sum,
            "本轮流水净额分": ledger_net,
            "本轮触及余额合计分": balance_sum,
            "全量流水净额分": all_ledger_net,
            "全量账户余额分": all_bal,
            "本轮隔离数": q_count,
        }
        now = _now()
        cur = conn.execute(
            """
            INSERT INTO verify_reports(
              settle_run_id, ledger_net_cents, balance_sum_cents,
              quarantine_count, consistent, detail_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settle_run_id,
                all_ledger_net,
                all_bal,
                q_count,
                consistent,
                json.dumps(detail, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
        rid = cur.lastrowid
        row = conn.execute("SELECT * FROM verify_reports WHERE id = ?", (rid,)).fetchone()
        out = dict(row)
        out["detail"] = detail
        return out
