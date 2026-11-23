"""离线灌点假事故夹具（BUGFIX-07 复现链）。

事故：值班员把探头标离线后，在批批次仍接受该探头新采样写入，
现场无读数温谱却继续叠点（幽灵曲线）。

正确性口径（离线采样纪要，2026-11-23 封口，单轨甲）：
- 测甲：探头心跳离线 → 新采样正式行零增并拒写，记「离线采样偏航」；
- 测乙：未晋升/被拒采样不得画入曲线，五页同源无幽灵点；
- 争用：两会话向同一离线探头灌点，探头互斥锁后到者只存痕。
"""
import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str) -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


# 幽灵采样点：数值合法可过量程校验，专供辨认是否被偷画入曲线
GHOST_POINTS = [{"t": "23:59", "c": -13.3}, {"t": "23:59:30", "c": -13.1}]
GHOST_BATCH_CODE = "TB-GHOST-001"


@pytest.fixture
def offline_incident():
    """假事故夹具：在批批次挂接的探头被值班员标离线，随后有人灌点。"""
    c = TestClient(app)
    admin = _auth(c, "admin")
    ops = _auth(c, "ops")
    chambers = c.get("/api/chambers", headers=admin).json()["items"]
    ch = next(x for x in chambers if x["chamber_code"] == "CK-HD-01")
    probes = c.get(f"/api/probes?chamber_id={ch['id']}", headers=admin).json()["items"]
    probe = next(p for p in probes if p["online_status"] == "在线")
    # 探头在线时正常入库一批，作为「在批批次」
    r = c.post(
        "/api/batches",
        headers=admin,
        json={
            "chamber_id": ch["id"],
            "probe_id": probe["id"],
            "cargo_ref": "假事故在批货载",
            "batch_code": "TB-INCIDENT-001",
            "temp_points": [{"t": "08:00", "c": -19.0}],
        },
    )
    assert r.status_code == 200, r.text
    # 值班员把探头标离线 —— 事故起点
    off = c.post(f"/api/probes/{probe['id']}/offline", headers=admin)
    assert off.status_code == 200, off.text
    with connect() as conn:
        rows_before = conn.execute("SELECT COUNT(*) c FROM temp_batches").fetchone()["c"]
    chamber_detail = c.get(f"/api/chambers/{ch['id']}", headers=admin).json()
    recon = c.get(f"/api/reconcile/{ch['chamber_code']}", headers=admin).json()
    cals = c.get("/api/calibrations", headers=admin).json()["items"]
    return {
        "client": c,
        "admin": admin,
        "ops": ops,
        "chamber": ch,
        "probe": probe,
        "rows_before": rows_before,
        "active_before": chamber_detail["active_batch_count"],
        "recon_active_before": recon["active_batches"],
        "cal_count_before": len(cals),
    }


def _pour_ghost(fx: dict, headers: dict):
    """向离线探头所属仓间灌幽灵采样点（新建批次写口）。"""
    return fx["client"].post(
        "/api/batches",
        headers=headers,
        json={
            "chamber_id": fx["chamber"]["id"],
            "probe_id": fx["probe"]["id"],
            "cargo_ref": "离线幽灵采样",
            "batch_code": GHOST_BATCH_CODE,
            "temp_points": GHOST_POINTS,
        },
    )


def _batch_rows() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) c FROM temp_batches").fetchone()["c"]


def _deviation_rows() -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='离线采样偏航'"
        ).fetchone()["c"]


def test_offline_pour_rejected_zero_new_rows(offline_incident):
    """测甲：探头心跳离线，新采样正式行零增并拒写，记离线采样偏航。"""
    fx = offline_incident
    r = _pour_ghost(fx, fx["ops"])
    assert r.status_code == 409, f"离线灌点未被拒写：{r.status_code} {r.text}"
    assert "离线" in (r.json().get("detail") or "")
    assert _batch_rows() == fx["rows_before"], "离线后正式采样行仍在增加"
    assert _deviation_rows() >= 1, "偏离纪要未记「离线采样偏航」"


def test_unpromoted_points_never_drawn(offline_incident):
    """测乙：被拒/待传采样不得画入曲线，五页同源无幽灵点。"""
    fx = offline_incident
    c = fx["client"]
    _pour_ghost(fx, fx["ops"])
    # 批次页（曲线数据源）：任何批次不得含幽灵点，幽灵批次不得存在
    items = c.get("/api/batches", headers=fx["admin"]).json()["items"]
    assert all(b["batch_code"] != GHOST_BATCH_CODE for b in items)
    for b in items:
        for pt in b["temp_points"]:
            assert pt not in GHOST_POINTS, "未晋升采样已被画入温谱曲线"
    # 探头页：心跳态同源显示离线
    probes = c.get("/api/probes", headers=fx["admin"]).json()["items"]
    me = next(p for p in probes if p["id"] == fx["probe"]["id"])
    assert me["online_status"] == "离线"
    # 仓间页：在批数未因幽灵写虚增
    detail = c.get(f"/api/chambers/{fx['chamber']['id']}", headers=fx["admin"]).json()
    assert detail["active_batch_count"] == fx["active_before"]
    # 对账页：在批差额同源且仍平衡
    recon = c.get(
        f"/api/reconcile/{fx['chamber']['chamber_code']}", headers=fx["admin"]
    ).json()
    assert recon["active_batches"] == fx["recon_active_before"]
    assert recon["balanced"] is True
    # 校准页：校准记录未被幽灵写触碰
    cals = c.get("/api/calibrations", headers=fx["admin"]).json()["items"]
    assert len(cals) == fx["cal_count_before"]


def test_two_sessions_contend_same_offline_probe(offline_incident):
    """争用：两会话同批抢灌点，探头互斥锁后到者只存痕、零业务写。"""
    fx = offline_incident
    c = fx["client"]
    probe_key = f"probe:{fx['probe']['id']}"
    # 会话一（admin）占住探头互斥锁
    hold = c.post(
        "/api/locks/acquire", headers=fx["admin"], json={"lock_key": probe_key}
    )
    assert hold.status_code == 200, hold.text
    # 会话二（ops）同批抢灌点 → 争用拒写，只存痕
    r = _pour_ghost(fx, fx["ops"])
    assert r.status_code == 409, f"争用未被互斥锁拦下：{r.status_code} {r.text}"
    assert "争用" in (r.json().get("detail") or "")
    cont = c.get("/api/contention", headers=fx["admin"]).json()["items"]
    assert any(
        x["lock_key"] == probe_key and x["actor"] == "ops" for x in cont
    ), "争用存痕丢失：contention_log 无该探头锁记录"
    assert _batch_rows() == fx["rows_before"], "争用方发生了业务写"
    # 会话一放行后，写口仍按单轨甲拒写并记偏航
    rel = c.post(
        "/api/locks/release", headers=fx["admin"], json={"lock_key": probe_key}
    )
    assert rel.status_code == 200, rel.text
    r2 = _pour_ghost(fx, fx["admin"])
    assert r2.status_code == 409, f"放行后离线灌点未被拒写：{r2.status_code}"
    assert "离线" in (r2.json().get("detail") or "")
    assert _batch_rows() == fx["rows_before"]
    assert _deviation_rows() >= 1
