"""隔声试验数据交付复核 Web 应用（FastAPI）。

流程：上传 SQLite 库 → 勾选试验 / 剔除异常读数 → 预览引用解析结果
（含跨组拉入链与冲突）→ 确认后下载 SQLite 子库。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import cotest
from .core import build_subset, load_model, resolve, validate_schema

DATA_DIR = os.environ.get("DATA_DIR", "./data")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))

app = FastAPI(title="隔声试验数据交付复核")
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
)


@dataclass
class SessionState:
    workdir: str
    source_name: str
    selected_tests: set[int] = field(default_factory=set)
    excluded_readings: set[int] = field(default_factory=set)
    # 同一会话的共测记录写操作串行化（跨进程由 SQLite BEGIN IMMEDIATE 兜底）
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def db_path(self) -> str:
        return os.path.join(self.workdir, "source.db")

    @property
    def cotest_path(self) -> str:
        return os.path.join(self.workdir, cotest.SIDECAR_NAME)


# 单进程内存会话；重启后若上传文件仍在 DATA_DIR，可凭原 cookie 自动恢复会话
SESSIONS: dict[str, SessionState] = {}


def _session_from_disk(sid: str) -> SessionState | None:
    """进程重启后按 cookie 找回仍保留的工作目录（不信任任意路径）。"""
    if not sid or not all(c in "0123456789abcdef" for c in sid):
        return None
    workdir = os.path.join(DATA_DIR, sid)
    db_path = os.path.join(workdir, "source.db")
    if not os.path.isfile(db_path):
        return None
    name_path = os.path.join(workdir, "source.name")
    name = "source.db"
    try:
        with open(name_path, encoding="utf-8") as f:
            stored = f.read().strip()
        if stored:
            name = stored
    except OSError:
        pass
    return SessionState(workdir, name)


def _get_session(request: Request) -> SessionState | None:
    sid = request.cookies.get("sid")
    if not sid:
        return None
    st = SESSIONS.get(sid)
    if st is None:
        st = _session_from_disk(sid)
        if st is not None:
            SESSIONS[sid] = st
    return st


def _load(st: SessionState):
    conn = sqlite3.connect(f"file:{st.db_path}?mode=ro", uri=True)
    try:
        return load_model(conn)
    finally:
        conn.close()


def _open_cotest(st: SessionState):
    return cotest.open_sidecar(st.cotest_path)


_MESSAGES = {
    "registered": ("ok", "共测记录已登记；受影响的复核结论已按新连通性重算。"),
    "withdrawn": ("ok", "共测记录已撤回；受影响的复核结论已按新连通性重算。"),
    "demo": ("ok", "演示共测记录已载入（同一试件支撑多段共测链）。"),
    "confirmed": ("ok", "已确认当前频点的比对结论。"),
}


def _compare_redirect(reference: int | None, code: str) -> RedirectResponse:
    target = "/compare"
    params = []
    if reference is not None:
        params.append(f"reference={reference}")
    if code:
        params.append(f"msg={code}")
    if params:
        target += "?" + "&".join(params)
    return RedirectResponse(target, status_code=303)


def _render_compare(
    request: Request,
    st: SessionState,
    model,
    reference: int | None,
    *,
    error: str | None = None,
    status_code: int = 200,
    flash_code: str | None = None,
    preserve: dict | None = None,
):
    """渲染共测链页面；preserve 用于回填提交失败的表单值。"""
    conn = _open_cotest(st)
    try:
        ref = cotest.resolve_reference(conn, model, reference)
        page = cotest.build_page(conn, model, ref)
    finally:
        conn.close()
    flash = _MESSAGES.get(flash_code) if flash_code else None
    return templates.TemplateResponse(
        request,
        "compare.html",
        {
            "page": page,
            "source": st.source_name,
            "flash": {"kind": flash[0], "text": flash[1]} if flash else None,
            "error": error,
            "form": preserve or {},
        },
        status_code=status_code,
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request, "upload.html", {"error": None}
    )


@app.post("/upload", response_class=HTMLResponse)
async def upload(request: Request, file: UploadFile):
    error: str | None = None
    if not file.filename:
        error = "未选择文件"
    data = await file.read()
    if error is None and len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        error = f"文件超过大小限制（{MAX_UPLOAD_MB} MB）"

    if error is None:
        # 旧会话的工作目录一并清理
        old = _get_session(request)
        if old:
            SESSIONS.pop(request.cookies.get("sid"), None)
            shutil.rmtree(old.workdir, ignore_errors=True)

        sid = uuid.uuid4().hex
        workdir = os.path.join(DATA_DIR, sid)
        os.makedirs(workdir, exist_ok=True)
        path = os.path.join(workdir, "source.db")
        with open(path, "wb") as f:
            f.write(data)
        with open(os.path.join(workdir, "source.name"), "w", encoding="utf-8") as f:
            f.write(file.filename or "source.db")
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            errors = validate_schema(conn)
            conn.close()
        except sqlite3.DatabaseError:
            errors = ["文件不是有效的 SQLite 数据库"]
        if errors:
            shutil.rmtree(workdir, ignore_errors=True)
            return templates.TemplateResponse(
                request,
                "upload.html",
                {"error": "；".join(errors)},
                status_code=400,
            )
        SESSIONS[sid] = SessionState(workdir, file.filename or "source.db")
        resp = RedirectResponse("/select", status_code=303)
        resp.set_cookie(
            "sid", sid, httponly=True, samesite="lax", max_age=30 * 24 * 3600
        )
        return resp

    return templates.TemplateResponse(
        request, "upload.html", {"error": error}, status_code=400
    )


# 读数表展示时的优先列序，其余列按出现顺序排后
_PREFERRED_READING_COLS = [
    "id", "kind", "frequency_hz", "level_db",
    "control_reading_id", "calibration_id", "note",
]


@app.get("/select", response_class=HTMLResponse)
def select_page(request: Request):
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)
    model = _load(st)

    headers: list[str] = []
    for r in model.readings.values():
        for col in r:
            if col not in headers:
                headers.append(col)
    headers = [c for c in _PREFERRED_READING_COLS if c in headers] + [
        c for c in headers if c not in _PREFERRED_READING_COLS
    ]

    groups = []
    for gid in sorted(model.groups):
        g = model.groups[gid]
        tests = []
        for t in sorted(model.tests.values(), key=lambda x: x["id"]):
            if t["group_id"] != gid:
                continue
            readings = [
                {
                    "id": r["id"],
                    "cells": [r.get(c) for c in headers],
                    "excluded": r["id"] in st.excluded_readings,
                }
                for r in (
                    model.readings[rid]
                    for rid in model.readings_by_test.get(t["id"], [])
                )
            ]
            tests.append(
                {"row": t, "readings": readings, "selected": t["id"] in st.selected_tests}
            )
        groups.append({"row": g, "tests": tests})

    return templates.TemplateResponse(
        request,
        "select.html",
        {
            "groups": groups,
            "headers": headers,
            "source": st.source_name,
        },
    )


@app.post("/select")
async def select_save(request: Request):
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    model = _load(st)

    selected = {int(v) for v in form.getlist("test")} & set(model.tests)
    # 剔除仅对所选试验内的读数生效，其余静默忽略
    excluded = {
        int(v)
        for v in form.getlist("excl")
        if (r := model.readings.get(int(v))) and r["test_id"] in selected
    }
    st.selected_tests = selected
    st.excluded_readings = excluded
    return RedirectResponse("/preview", status_code=303)


@app.get("/preview", response_class=HTMLResponse)
def preview(request: Request):
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)
    model = _load(st)
    res = resolve(model, st.selected_tests, st.excluded_readings)
    return templates.TemplateResponse(
        request,
        "preview.html",
        {
            "res": res,
            "model": model,
            "selected": sorted(st.selected_tests),
            "excluded": sorted(st.excluded_readings),
            "pulled_in": sorted(res.pulled_in.items()),
            "source": st.source_name,
        },
    )


@app.get("/compare", response_class=HTMLResponse)
def compare_page(request: Request, reference: str | None = None, msg: str = ""):
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)
    model = _load(st)
    ref: int | None = None
    try:
        ref = int(reference) if reference not in (None, "") else None
    except ValueError:
        ref = None
    return _render_compare(request, st, model, ref, flash_code=msg)


def _form_int(form, name: str) -> int | None:
    try:
        value = form.get(name)
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _form_int_query(request: Request) -> int | None:
    try:
        value = request.query_params.get("reference")
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


@app.post("/compare/records")
async def add_record(request: Request):
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    reference = _form_int(form, "reference")
    preserve = {
        "specimen": str(form.get("specimen") or ""),
        "lab_a": str(form.get("lab_a") or ""),
        "reading_a": str(form.get("reading_a") or ""),
        "lab_b": str(form.get("lab_b") or ""),
        "reading_b": str(form.get("reading_b") or ""),
    }
    model = _load(st)
    try:
        lab_a = _form_int(form, "lab_a")
        lab_b = _form_int(form, "lab_b")
        reading_a = _form_int(form, "reading_a")
        reading_b = _form_int(form, "reading_b")
        if None in (lab_a, lab_b, reading_a, reading_b):
            raise cotest.ValidationError(
                ["请填写两家实验室及各自的读数编号（整数）"]
            )
        with st.lock:
            conn = _open_cotest(st)
            try:
                cotest.add_record(
                    conn,
                    model,
                    preserve["specimen"],
                    lab_a,
                    reading_a,
                    lab_b,
                    reading_b,
                )
            finally:
                conn.close()
    except cotest.ValidationError as exc:
        return _render_compare(
            request,
            st,
            model,
            reference,
            error="；".join(exc.errors),
            status_code=400,
            preserve=preserve,
        )
    return _compare_redirect(reference, "registered")


@app.post("/compare/records/{record_id}/withdraw")
def withdraw_record(request: Request, record_id: int):
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)
    model = _load(st)
    reference = _form_int_query(request)
    try:
        with st.lock:
            conn = _open_cotest(st)
            try:
                cotest.withdraw_record(conn, model, record_id)
            finally:
                conn.close()
    except cotest.ValidationError as exc:
        return _render_compare(
            request,
            st,
            model,
            reference,
            error="；".join(exc.errors),
            status_code=409,
        )
    return _compare_redirect(reference, "withdrawn")


@app.post("/compare/confirm")
async def confirm(request: Request):
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    model = _load(st)
    reference = _form_int(form, "reference")
    frequency = str(form.get("frequency") or "")
    token = str(form.get("token") or "")
    try:
        if reference is None:
            raise cotest.ValidationError(["缺少参照实验室"])
        try:
            frequency_hz = float(frequency)
        except ValueError:
            raise cotest.ValidationError(["频点无效"])
        with st.lock:
            conn = _open_cotest(st)
            try:
                cotest.confirm_conclusion(
                    conn, model, reference, frequency_hz, token
                )
            finally:
                conn.close()
    except cotest.StaleConclusion as exc:
        return _render_compare(
            request,
            st,
            model,
            exc.current.reference_lab,
            error="；".join(exc.errors),
            status_code=409,
        )
    except cotest.ValidationError as exc:
        return _render_compare(
            request,
            st,
            model,
            reference,
            error="；".join(exc.errors),
            status_code=400,
        )
    return _compare_redirect(reference, "confirmed")


@app.post("/compare/seed-demo")
def seed_demo(request: Request):
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)
    model = _load(st)
    reference = _form_int_query(request)
    try:
        with st.lock:
            conn = _open_cotest(st)
            try:
                cotest.seed_demo(conn, model)
            finally:
                conn.close()
    except cotest.ValidationError as exc:
        return _render_compare(
            request,
            st,
            model,
            reference,
            error="；".join(exc.errors),
            status_code=400,
        )
    return _compare_redirect(reference, "demo")


@app.post("/download")
def download(request: Request):
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)
    model = _load(st)
    # 下载前重新解析，防止预览之后状态被修改导致不一致
    res = resolve(model, st.selected_tests, st.excluded_readings)
    if not res.ok:
        return HTMLResponse(
            "<h2>已阻止下载</h2><p>存在未解决的引用冲突（悬空或指向已剔除读数）。"
            "请返回 <a href='/preview'>预览</a> 查看引用链，"
            "或回 <a href='/select'>选择页</a> 修改。</p>",
            status_code=409,
        )
    out_path = os.path.join(st.workdir, f"subset_{int(time.time() * 1000)}.db")
    build_subset(st.db_path, out_path, res)
    name = os.path.splitext(st.source_name)[0] + "_subset.db"
    return FileResponse(
        out_path, filename=name, media_type="application/x-sqlite3"
    )
