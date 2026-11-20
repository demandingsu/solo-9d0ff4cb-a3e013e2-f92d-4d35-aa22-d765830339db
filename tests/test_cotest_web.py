"""共测链网页端到端测试：登记 / 撤回 / 交错确认 / 事务回滚。"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", str(tmp_path / "data"))
    main.SESSIONS.clear()
    return TestClient(main.app)


def _upload(client, sample_db):
    with open(sample_db, "rb") as f:
        resp = client.post(
            "/upload",
            files={"file": ("lab.db", f, "application/x-sqlite3")},
            follow_redirects=False,
        )
    assert resp.status_code == 303


def _session(client):
    sid = client.cookies.get("sid")
    return main.SESSIONS[sid]


def _sidecar(client):
    from app import cotest

    return cotest.open_sidecar(_session(client).cotest_path)


def _seed(client):
    return client.post("/compare/seed-demo", follow_redirects=False)


def _add(client, specimen, lab_a, reading_a, lab_b, reading_b, reference=None):
    data = {
        "specimen": specimen,
        "lab_a": str(lab_a),
        "reading_a": str(reading_a),
        "lab_b": str(lab_b),
        "reading_b": str(reading_b),
    }
    if reference is not None:
        data["reference"] = str(reference)
    return client.post(
        "/compare/records",
        data=data,
        follow_redirects=False,
    )


def _confirm(client, reference, frequency, text=None):
    page = client.get(f"/compare?reference={reference}").text
    token = None
    marker = f'name="frequency" value="{frequency}"'
    pos = page.find(marker)
    assert pos != -1, f"页面未出现 {frequency} Hz 的确认表单"
    token_pos = page.find('name="token" value="', pos)
    token = page[token_pos + len('name="token" value="'):].split('"', 1)[0]
    return client.post(
        "/compare/confirm",
        data={"reference": str(reference), "frequency": frequency, "token": token},
        follow_redirects=False,
    )


def test_compare_page_requires_session(client):
    assert client.get("/compare", follow_redirects=False).status_code == 303


def test_seed_compliant_demo_and_render(client, sample_db):
    _upload(client, sample_db)
    resp = client.get("/compare")
    assert resp.status_code == 200
    assert "载入演示共测记录" in resp.text

    assert _seed(client).status_code == 303
    page = client.get("/compare?reference=1")
    assert page.status_code == 200
    assert "S-墙体-01" in page.text
    assert "关键试件" in page.text
    assert "100 Hz" in page.text and "125 Hz" in page.text

    body = page.text
    at125 = body[body.find("125 Hz ｜"):]
    # 125 Hz 下撤掉 S-墙体-01 会同时断开 B、C
    assert "墙体试件B" in at125 and "公共设施" in at125


def test_register_and_withdraw_roundtrip(client, sample_db):
    _upload(client, sample_db)
    resp = _add(client, "S-任意", 1, 101, 2, 201)
    assert resp.status_code == 303
    assert "S-任意" in client.get("/compare").text

    conn = _sidecar(client)
    from app import cotest

    rec = next(r for r in cotest.list_records(conn) if r.specimen == "S-任意")
    conn.close()
    resp = client.post(f"/compare/records/{rec.id}/withdraw", follow_redirects=False)
    assert resp.status_code == 303
    page = client.get("/compare").text
    assert "S-任意" in page
    assert "已撤回记录" in page


def test_register_rejects_desynced_frequency(client, sample_db):
    _upload(client, sample_db)
    resp = _add(client, "S-错频", 1, 101, 2, 202)
    assert resp.status_code == 400
    assert "不同频" in resp.text
    conn = _sidecar(client)
    from app import cotest

    assert cotest.list_records(conn) == []
    conn.close()


def test_registration_invalidates_only_affected_confirmations(client, sample_db):
    _upload(client, sample_db)
    _seed(client)
    assert _confirm(client, 1, "100").status_code == 303
    assert _confirm(client, 1, "125").status_code == 303
    assert "已确认" in client.get("/compare?reference=1").text

    # 撤回 125 Hz 的一条记录 → 125 Hz 回到待复核，100 Hz 确认仍在
    conn = _sidecar(client)
    from app import cotest

    rec = next(
        r for r in cotest.list_records(conn) if r.frequency_hz == 125.0 and r.lab_a == 1
    )
    conn.close()
    assert client.post(
        f"/compare/records/{rec.id}/withdraw", follow_redirects=False
    ).status_code == 303

    body = client.get("/compare?reference=1").text
    block100 = body[body.find("100 Hz ｜"):body.find("125 Hz ｜")]
    block125 = body[body.find("125 Hz ｜"):]
    assert "已确认" in block100
    assert "确认所见结论" in block125


def test_stale_page_cannot_confirm_changed_conclusion(client, sample_db):
    _upload(client, sample_db)
    _seed(client)

    # 旧页面：抓取 125 Hz 的确认令牌
    old_page = client.get("/compare?reference=1").text
    block = old_page[old_page.find("125 Hz ｜"):]
    token = block.split('name="token" value="', 1)[1].split('"', 1)[0]

    # 页面打开之后撤回 125 Hz 的 A—B 记录，125 Hz 结论改变
    conn = _sidecar(client)
    from app import cotest

    rec = next(
        r for r in cotest.list_records(conn) if r.frequency_hz == 125.0 and r.lab_a == 1
    )
    conn.close()
    client.post(f"/compare/records/{rec.id}/withdraw", follow_redirects=False)

    resp = client.post(
        "/compare/confirm",
        data={"reference": "1", "frequency": "125", "token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 409
    assert "旧页面不能确认" in resp.text
    conn = _sidecar(client)
    assert (1, 125.0) not in cotest.review_states(conn)
    conn.close()


def test_confirm_changed_conclusion_after_adding_redundancy(client, sample_db):
    """新增冗余记录改变关键试件清单后，旧页面令牌同样失效。"""
    _upload(client, sample_db)
    _seed(client)
    old_page = client.get("/compare?reference=1").text
    block = old_page[old_page.find("125 Hz ｜"):]
    token = block.split('name="token" value="', 1)[1].split('"', 1)[0]

    # 增加 A—C 直连后，S-墙体-01 不再让 C 断链 → 关键试件清单变化
    assert _add(client, "S-墙体-02", 1, 102, 3, 902).status_code == 303
    resp = client.post(
        "/compare/confirm",
        data={"reference": "1", "frequency": "125", "token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 409
    assert "旧页面不能确认" in resp.text


def test_withdrawing_all_records_at_frequency_clears_confirmation(client, sample_db):
    """确认后该频点记录被全部撤回：结论变化，确认失效，频点不再出现在结论区。"""
    _upload(client, sample_db)
    _seed(client)
    assert _confirm(client, 1, "125").status_code == 303

    conn = _sidecar(client)
    from app import cotest

    ids = [r.id for r in cotest.list_records(conn) if r.frequency_hz == 125.0]
    conn.close()
    for record_id in ids:
        client.post(f"/compare/records/{record_id}/withdraw", follow_redirects=False)

    body = client.get("/compare?reference=1").text
    assert "125 Hz ｜" not in body
    conn = _sidecar(client)
    assert (1, 125.0) not in cotest.review_states(conn)
    conn.close()


def test_failed_registration_keeps_original_state(client, sample_db, monkeypatch):
    _upload(client, sample_db)
    _seed(client)
    assert _confirm(client, 1, "100").status_code == 303

    from app import cotest

    conn = _sidecar(client)
    before_records = cotest.list_records(conn, include_withdrawn=True)
    before_reviews = cotest.review_states(conn)
    conn.close()

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("模拟刷新失败")

    monkeypatch.setattr(main.cotest, "refresh_reviews", boom)
    with pytest.raises(sqlite3.OperationalError):
        _add(client, "S-失败", 1, 101, 2, 201)

    conn = _sidecar(client)
    assert cotest.list_records(conn, include_withdrawn=True) == before_records
    assert cotest.review_states(conn) == before_reviews
    conn.close()


def test_session_survives_restart_with_cookie(client, sample_db):
    """进程重启（内存会话清空）后，凭原 cookie 与保留的工作目录继续访问共测记录。"""
    _upload(client, sample_db)
    _seed(client)
    sid = client.cookies.get("sid")

    main.SESSIONS.clear()  # 模拟重启
    resp = client.get("/compare?reference=1")
    assert resp.status_code == 200
    assert "S-墙体-01" in resp.text

    # 伪造的 session id 不能被当成路径使用
    client.cookies.clear()
    client.cookies.set("sid", "../etc")
    assert client.get("/compare", follow_redirects=False).status_code == 303


def test_records_are_stored_outside_source_db(client, sample_db):
    """共测记录写入旁路库，不改写上传的源库。"""
    import hashlib

    _upload(client, sample_db)
    st = _session(client)
    before = hashlib.sha256(open(st.db_path, "rb").read()).hexdigest()
    _seed(client)
    after = hashlib.sha256(open(st.db_path, "rb").read()).hexdigest()
    assert before == after
    assert _session(client).cotest_path.endswith("cotest.db")


def test_withdraw_twice_reports_conflict(client, sample_db):
    _upload(client, sample_db)
    _add(client, "S-一次性", 1, 101, 2, 201)
    conn = _sidecar(client)
    from app import cotest

    rec = next(r for r in cotest.list_records(conn) if r.specimen == "S-一次性")
    conn.close()
    assert client.post(
        f"/compare/records/{rec.id}/withdraw", follow_redirects=False
    ).status_code == 303
    resp = client.post(f"/compare/records/{rec.id}/withdraw", follow_redirects=False)
    assert resp.status_code == 409
    assert "已经撤回" in resp.text


def test_reference_forms_keep_selection(client, sample_db):
    _upload(client, sample_db)
    _seed(client)
    resp = _add(client, "S-新", 1, 101, 2, 201, reference=2)
    assert "/compare?" in resp.headers["location"]
    assert "reference=2" in resp.headers["location"]
    # 页面切换参照不影响记录本身
    assert client.get("/compare?reference=2").status_code == 200
    # 无效参照回落为默认实验室
    page = client.get("/compare?reference=999")
    assert page.status_code == 200
    assert "#1「墙体试件A」" in page.text
