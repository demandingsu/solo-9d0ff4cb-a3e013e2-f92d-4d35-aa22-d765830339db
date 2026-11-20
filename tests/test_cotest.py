"""共测链核心逻辑测试：同频校验、连通性、关键试件、复核状态与事务回滚。"""
import sqlite3

import pytest

from app import cotest


def _sidecar(tmp_path, model):
    return cotest.open_sidecar(str(tmp_path / "cotest.db"))


def _seed(tmp_path, model):
    conn = _sidecar(tmp_path, model)
    cotest.seed_demo(conn, model)
    return conn


def test_demo_records_are_same_frequency(tmp_path, model):
    conn = _seed(tmp_path, model)
    records = cotest.list_records(conn)
    assert len(records) == 5
    assert {r.frequency_hz for r in records} == {100.0, 125.0}
    # 同一试件在第一段记录上同时支撑 A—B 与 B—C
    segments = {
        (r.frequency_hz, r.lab_a, r.lab_b)
        for r in records
        if r.specimen == "S-墙体-01"
    }
    assert (100.0, 1, 2) in segments and (100.0, 2, 3) in segments
    assert (125.0, 1, 2) in segments and (125.0, 2, 3) in segments


def test_add_record_rejects_different_frequency(model, tmp_path):
    conn = _sidecar(tmp_path, model)
    # R101 为 100 Hz，R202 为 125 Hz
    with pytest.raises(cotest.ValidationError) as exc:
        cotest.add_record(conn, model, "S-X", 1, 101, 2, 202)
    assert any("不同频" in e for e in exc.value.errors)
    assert cotest.list_records(conn) == []


def test_add_record_rejects_reading_of_other_lab(model, tmp_path):
    conn = _sidecar(tmp_path, model)
    with pytest.raises(cotest.ValidationError) as exc:
        # 读数 101 属于实验室 #1，却登记为实验室 #2 的读数
        cotest.add_record(conn, model, "S-X", 1, 101, 2, 101)
    assert any("不属于" in e for e in exc.value.errors)


def test_add_record_rejects_same_lab_and_missing_specimen(model, tmp_path):
    conn = _sidecar(tmp_path, model)
    with pytest.raises(cotest.ValidationError) as exc:
        cotest.add_record(conn, model, "  ", 1, 101, 1, 102)
    messages = "；".join(exc.value.errors)
    assert "试件号" in messages and "不能相同" in messages


def test_duplicate_record_rejected(model, tmp_path):
    conn = _seed(tmp_path, model)
    with pytest.raises(cotest.ValidationError) as exc:
        cotest.add_record(conn, model, "S-墙体-01", 2, 201, 1, 101)  # 反向重复
    assert any("已有有效共测记录" in e for e in exc.value.errors)


def test_connectivity_by_frequency(tmp_path, model):
    conn = _sidecar(tmp_path, model)
    # 仅登记 100 Hz 的 A—B，参照 A 时 B 可比、C 不可比
    cotest.add_record(conn, model, "S-1", 1, 101, 2, 201)
    at100 = cotest.conclusion(conn, model, 1, 100)
    assert [p.lab_id for p in at100.comparable] == [2]
    at125 = cotest.conclusion(conn, model, 1, 125)
    assert at125.comparable == ()


def test_same_specimen_supports_multi_segment_chain(tmp_path, model):
    conn = _seed(tmp_path, model)
    at125 = cotest.conclusion(conn, model, 1, 125)
    paths = {p.lab_id: p for p in at125.comparable}
    assert set(paths) == {2, 3}
    # 125 Hz 到 C 必须走两段，两段同属 S-墙体-01
    assert [s.specimen for s in paths[3].path] == ["S-墙体-01", "S-墙体-01"]
    assert [(s.from_lab, s.to_lab) for s in paths[3].path] == [(1, 2), (2, 3)]


def test_critical_specimen_depends_on_frequency(tmp_path, model):
    conn = _seed(tmp_path, model)
    at100 = cotest.conclusion(conn, model, 1, 100)
    # 100 Hz 有 S-墙体-02 提供 A—C 直连冗余，只有 B 断链
    assert [(c.specimen, c.broken_labs) for c in at100.critical] == [
        ("S-墙体-01", (2,))
    ]
    at125 = cotest.conclusion(conn, model, 1, 125)
    # 125 Hz 无冗余，B、C 都断链
    assert [(c.specimen, c.broken_labs) for c in at125.critical] == [
        ("S-墙体-01", (2, 3))
    ]


def test_critical_uses_all_segments_of_specimen(tmp_path, model):
    conn = _seed(tmp_path, model)
    at125 = cotest.conclusion(conn, model, 1, 125)
    assert set(at125.critical[0].record_ids) == {4, 5}


def test_non_critical_specimen_excluded(tmp_path, model):
    conn = _sidecar(tmp_path, model)
    cotest.add_record(conn, model, "S-桥", 1, 101, 2, 201)
    cotest.add_record(conn, model, "S-备用", 1, 101, 2, 201)
    result = cotest.conclusion(conn, model, 1, 100)
    # B 有两条不同试件的共测链；撤掉任一单个试件仍可经另一条到达
    assert result.critical == ()


def test_reference_change_changes_conclusion(tmp_path, model):
    conn = _seed(tmp_path, model)
    a = cotest.conclusion(conn, model, 1, 125)
    b = cotest.conclusion(conn, model, 2, 125)
    assert a.fingerprint != b.fingerprint
    # 参照 B 时 C 仍可达，但 A 的链方向相反
    assert {p.lab_id for p in b.comparable} == {1, 3}


def test_fingerprint_stable_under_unrelated_change(tmp_path, model):
    conn = _seed(tmp_path, model)
    before = cotest.conclusion(conn, model, 1, 100).fingerprint
    # 增删另一频点的记录不影响 100 Hz 结论
    cotest.add_record(conn, model, "S-其他", 1, 102, 2, 202)  # 125 Hz
    assert cotest.conclusion(conn, model, 1, 100).fingerprint == before


def test_confirm_and_revoke_on_change(tmp_path, model):
    conn = _seed(tmp_path, model)
    current = cotest.conclusion(conn, model, 1, 100)
    cotest.confirm_conclusion(conn, model, 1, 100, current.fingerprint)
    assert cotest.review_states(conn)[(1, 100.0)]["fingerprint"] == current.fingerprint

    # 撤掉冗余试件 S-墙体-02：A—C 直连消失，C 变为由 S-墙体-01 支撑，
    # 关键试件清单随之变化 → 确认失效
    rec = next(r for r in cotest.list_records(conn) if r.specimen == "S-墙体-02")
    cotest.withdraw_record(conn, model, rec.id)
    assert (1, 100.0) not in cotest.review_states(conn)


def test_withdrawal_keeps_unchanged_confirmations(tmp_path, model):
    conn = _seed(tmp_path, model)
    c100 = cotest.conclusion(conn, model, 1, 100)
    c125 = cotest.conclusion(conn, model, 1, 125)
    cotest.confirm_conclusion(conn, model, 1, 100, c100.fingerprint)
    cotest.confirm_conclusion(conn, model, 1, 125, c125.fingerprint)

    # 撤回 125 Hz 的一条记录：125 Hz 结论变化，100 Hz 不受影响
    rec = next(
        r for r in cotest.list_records(conn) if r.frequency_hz == 125.0 and r.lab_a == 1
    )
    cotest.withdraw_record(conn, model, rec.id)
    states = cotest.review_states(conn)
    assert (1, 100.0) in states
    assert (1, 125.0) not in states


def test_confirm_rejects_stale_token(tmp_path, model):
    conn = _seed(tmp_path, model)
    stale = cotest.conclusion(conn, model, 1, 100).fingerprint
    rec = next(r for r in cotest.list_records(conn) if r.specimen == "S-墙体-02")
    cotest.withdraw_record(conn, model, rec.id)
    with pytest.raises(cotest.StaleConclusion) as exc:
        cotest.confirm_conclusion(conn, model, 1, 100, stale)
    assert exc.value.current.fingerprint != stale
    assert cotest.review_states(conn) == {}


def test_withdraw_unknown_record_rolls_back(tmp_path, model):
    conn = _seed(tmp_path, model)
    before = cotest.list_records(conn, include_withdrawn=True)
    with pytest.raises(cotest.ValidationError):
        cotest.withdraw_record(conn, model, 99999)
    after = cotest.list_records(conn, include_withdrawn=True)
    assert before == after


def test_failed_refresh_rolls_back_insert(tmp_path, model, monkeypatch):
    """写记录与刷新复核状态同事务：刷新失败则记录不落库。"""
    conn = _seed(tmp_path, model)
    before = cotest.list_records(conn, include_withdrawn=True)

    def boom(*args, **kwargs):
        raise RuntimeError("刷新失败")

    monkeypatch.setattr(cotest, "refresh_reviews", boom)
    with pytest.raises(RuntimeError):
        cotest.add_record(conn, model, "S-新", 1, 101, 2, 201)
    assert cotest.list_records(conn, include_withdrawn=True) == before


def test_confirmation_survives_unrelated_registration(tmp_path, model):
    conn = _seed(tmp_path, model)
    c100 = cotest.conclusion(conn, model, 1, 100)
    cotest.confirm_conclusion(conn, model, 1, 100, c100.fingerprint)
    # 在 160 Hz 新增 A—C 共测（R103、R903 均 160 Hz），不影响 100 Hz 结论
    cotest.add_record(conn, model, "S-160", 1, 103, 3, 903)
    assert (1, 100.0) in cotest.review_states(conn)
    assert cotest.conclusion(conn, model, 1, 100).fingerprint == c100.fingerprint


def test_any_record_change_invalidates_same_frequency(tmp_path, model):
    """同频点新增/撤回任何记录都会让旧令牌失效，即便可比集合暂未改变。"""
    conn = _seed(tmp_path, model)
    c125 = cotest.conclusion(conn, model, 1, 125)
    cotest.confirm_conclusion(conn, model, 1, 125, c125.fingerprint)
    # S-墙体-02 在 125 Hz 与 A—C 直连，新增后 C 仍可比、关键试件清单不变
    cotest.add_record(conn, model, "S-墙体-02", 1, 102, 3, 902)
    assert (1, 125.0) not in cotest.review_states(conn)


def test_withdrawn_record_ignored_in_connectivity(tmp_path, model):
    conn = _seed(tmp_path, model)
    for r in cotest.list_records(conn):
        if r.frequency_hz == 125.0:
            cotest.withdraw_record(conn, model, r.id)
    at125 = cotest.conclusion(conn, model, 1, 125)
    assert at125.comparable == () and at125.critical == ()


def test_sidecar_survives_reopen(tmp_path, model):
    path = str(tmp_path / "cotest.db")
    conn = cotest.open_sidecar(path)
    cotest.seed_demo(conn, model)
    conn.close()
    again = cotest.open_sidecar(path)
    assert len(cotest.list_records(again)) == 5
    again.close()
