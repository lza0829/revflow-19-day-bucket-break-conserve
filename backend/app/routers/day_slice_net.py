from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.modules import day_slice_net as svc

router = APIRouter(tags=["日切片净额"])


class RecalcIn(BaseModel):
    account_id: int
    day: str  # 入账日 YYYY-MM-DD


@router.post("/day-recalc-runs")
def post_recalc(body: RecalcIn):
    """按入账日重算某账户当日净贡献并写回；日桶不过则整次回滚（409）。"""
    return svc.recalc_day(body.account_id, body.day)


@router.get("/day-recalc-runs")
def get_runs():
    return svc.list_runs()


@router.get("/day-recalc-runs/{run_id}")
def get_run(run_id: int):
    return svc.get_run(run_id)


@router.get("/day-contributions")
def get_contributions(account_id: int | None = Query(None)):
    return svc.list_contributions(account_id)


@router.get("/settle-runs/{run_id}/conserve-proofs")
def get_conserve_proofs(run_id: int):
    """按归并运行 id 只读查询守恒证明（各日净贡献、全日合计、正冲净额、差额）。"""
    return svc.list_conserve_proofs(run_id)
