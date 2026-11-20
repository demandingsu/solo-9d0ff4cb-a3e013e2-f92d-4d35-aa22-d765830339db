"""共测记录、共测链连通性与复核状态的核心逻辑。

数据模型约定
------------
- 实验室沿用源库 ``groups`` 表（每组视为一家实验室的试验组，组名即实验室名）；
- 每条共测记录登记：试件号、两家实验室、两家实验室在库中的同频读数编号；
- 读数频率由读数自身 ``frequency_hz`` 读出，登记时强制两条读数同频；
- 共测记录与复核状态存放在上传会话目录下的旁路 SQLite 库 ``cotest.db``，
  不改写实验室上传的源库。

每个频点以参照实验室为起点，沿该频点的有效共测记录做连通性分析：

- 能经共测链到达的实验室即可与参照实验室比对；
- 对每个试件，临时撤掉它在当前频点的全部共测记录后重新计算连通性，
  若有当前可比的实验室因此断链，该试件即“关键试件”；
- 结论指纹由“参照实验室 + 频点 + 该频点全部有效记录 + 可比实验室集合 +
  关键试件及其断链实验室”共同决定。指纹变化即结论变化，
  对应复核确认作废，其余频点/参照的确认保留。

所有写操作（登记、撤回、确认）都在 ``BEGIN IMMEDIATE`` 事务中完成：
登记/撤回与复核状态刷新同事务提交，任何一步失败都回滚，原记录与复核状态不变。
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from .core import Model

SIDECAR_NAME = "cotest.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS co_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    specimen TEXT NOT NULL,
    lab_a INTEGER NOT NULL,
    reading_a INTEGER NOT NULL,
    lab_b INTEGER NOT NULL,
    reading_b INTEGER NOT NULL,
    frequency_hz REAL NOT NULL,
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    withdrawn_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cotest_freq ON co_tests(frequency_hz, active);
CREATE INDEX IF NOT EXISTS idx_cotest_specimen ON co_tests(specimen, active);
CREATE TABLE IF NOT EXISTS review_state (
    reference_lab INTEGER NOT NULL,
    frequency_hz REAL NOT NULL,
    fingerprint TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY (reference_lab, frequency_hz)
);
"""


class ValidationError(Exception):
    """登记/撤回/确认参数不合法；errors 为可直接展示的中文说明。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("；".join(errors))


class StaleConclusion(ValidationError):
    """确认时页面上的结论指纹与当前结论不一致（旧页面）。"""

    def __init__(self, current: "FrequencyConclusion"):
        self.current = current
        super().__init__(
            ["该结论在页面打开后已发生变化，旧页面不能确认已变动的结论。"
             "请核对当前结论后重新确认。"]
        )


@dataclass(frozen=True)
class CoTestRecord:
    id: int
    specimen: str
    lab_a: int
    reading_a: int
    lab_b: int
    reading_b: int
    frequency_hz: float
    created_at: str
    active: bool
    withdrawn_at: str | None


@dataclass(frozen=True)
class ChainStep:
    """共测链上的一环：沿一条共测记录从一家实验室走到另一家。"""

    record_id: int
    specimen: str
    from_lab: int
    to_lab: int
    from_reading: int
    to_reading: int


@dataclass(frozen=True)
class LabPath:
    lab_id: int
    path: tuple[ChainStep, ...]


@dataclass(frozen=True)
class CriticalSpecimen:
    specimen: str
    broken_labs: tuple[int, ...]
    record_ids: tuple[int, ...]


@dataclass(frozen=True)
class FrequencyConclusion:
    reference_lab: int
    frequency_hz: float
    comparable: tuple[LabPath, ...]
    critical: tuple[CriticalSpecimen, ...]
    fingerprint: str


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def canonical_frequency(value: float) -> float:
    """把频点规整到稳定表示，避免 100 与 100.00000000000001 分到两组。"""
    return float(f"{float(value):.12g}")


def open_sidecar(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    return conn


def _begin(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")


def _row_to_record(row: sqlite3.Row) -> CoTestRecord:
    return CoTestRecord(
        id=row["id"],
        specimen=row["specimen"],
        lab_a=row["lab_a"],
        reading_a=row["reading_a"],
        lab_b=row["lab_b"],
        reading_b=row["reading_b"],
        frequency_hz=float(row["frequency_hz"]),
        created_at=row["created_at"],
        active=bool(row["active"]),
        withdrawn_at=row["withdrawn_at"],
    )


def list_records(
    conn: sqlite3.Connection, *, include_withdrawn: bool = False
) -> list[CoTestRecord]:
    sql = "SELECT * FROM co_tests"
    if not include_withdrawn:
        sql += " WHERE active = 1"
    sql += " ORDER BY frequency_hz, specimen, id"
    return [_row_to_record(r) for r in conn.execute(sql)]


def _reading_lab(model: Model, reading_id: int) -> int | None:
    reading = model.readings.get(reading_id)
    if reading is None:
        return None
    test = model.tests.get(reading["test_id"])
    if test is None:
        return None
    return test.get("group_id")


def _reading_frequency(model: Model, reading_id: int) -> float | None:
    reading = model.readings.get(reading_id)
    if reading is None or reading.get("frequency_hz") is None:
        return None
    try:
        value = float(reading["frequency_hz"])
    except (TypeError, ValueError):
        return None
    return canonical_frequency(value) if math.isfinite(value) else None


def validate_candidate(
    model: Model,
    specimen: str,
    lab_a: int,
    reading_a: int,
    lab_b: int,
    reading_b: int,
) -> tuple[str, float]:
    """校验一条待登记记录，返回 (规整后的试件号, 频点)。"""
    errors: list[str] = []
    specimen = (specimen or "").strip()
    if not specimen:
        errors.append("试件号不能为空")
    elif len(specimen) > 200:
        errors.append("试件号过长（最多 200 字符）")

    if lab_a == lab_b:
        errors.append("两家实验室不能相同")
    for label, lab in (("实验室 A", lab_a), ("实验室 B", lab_b)):
        if lab not in model.groups:
            errors.append(f"{label}不存在（组 #{lab}）")

    freqs: dict[str, float] = {}
    for label, lab, reading_id in (
        ("读数 A", lab_a, reading_a),
        ("读数 B", lab_b, reading_b),
    ):
        if reading_id not in model.readings:
            errors.append(f"{label} #{reading_id} 不在库中")
            continue
        owner = _reading_lab(model, reading_id)
        if owner != lab:
            errors.append(f"{label} #{reading_id} 不属于{label[:-1]}实验室（组 #{lab}）")
        freq = _reading_frequency(model, reading_id)
        if freq is None:
            errors.append(f"{label} #{reading_id} 缺少有效频率，无法核对同频")
        else:
            freqs[label] = freq

    if len(freqs) == 2 and freqs["读数 A"] != freqs["读数 B"]:
        errors.append(
            f"两条读数不同频（读数 A {freqs['读数 A']:g} Hz，"
            f"读数 B {freqs['读数 B']:g} Hz）"
        )

    if errors:
        raise ValidationError(errors)
    return specimen, freqs["读数 A"]


def _duplicate_exists(
    conn: sqlite3.Connection,
    specimen: str,
    frequency_hz: float,
    lab_a: int,
    lab_b: int,
) -> bool:
    row = conn.execute(
        """SELECT 1 FROM co_tests
           WHERE active = 1 AND specimen = ? AND frequency_hz = ?
             AND ((lab_a = ? AND lab_b = ?) OR (lab_a = ? AND lab_b = ?))""",
        (specimen, frequency_hz, lab_a, lab_b, lab_b, lab_a),
    ).fetchone()
    return row is not None


def _insert(
    conn: sqlite3.Connection,
    specimen: str,
    lab_a: int,
    reading_a: int,
    lab_b: int,
    reading_b: int,
    frequency_hz: float,
    now: str,
) -> int:
    if _duplicate_exists(conn, specimen, frequency_hz, lab_a, lab_b):
        raise ValidationError(
            ["同一试件、同一频点、同一对实验室已有有效共测记录；"
             "如需修改请先撤回原记录"]
        )
    cur = conn.execute(
        """INSERT INTO co_tests
           (specimen, lab_a, reading_a, lab_b, reading_b,
            frequency_hz, created_at, active)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
        (specimen, lab_a, reading_a, lab_b, reading_b, frequency_hz, now),
    )
    return int(cur.lastrowid)


def add_record(
    conn: sqlite3.Connection,
    model: Model,
    specimen: str,
    lab_a: int,
    reading_a: int,
    lab_b: int,
    reading_b: int,
    *,
    now: str | None = None,
) -> int:
    """登记一条共测记录，并在同一事务内刷新受影响频点的复核状态。"""
    specimen, frequency_hz = validate_candidate(
        model, specimen, lab_a, reading_a, lab_b, reading_b
    )
    now = now or _now()
    _begin(conn)
    try:
        record_id = _insert(
            conn, specimen, lab_a, reading_a, lab_b, reading_b, frequency_hz, now
        )
        refresh_reviews(conn, model)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return record_id


def add_records(
    conn: sqlite3.Connection,
    model: Model,
    candidates: list[tuple[str, int, int, int, int]],
    *,
    now: str | None = None,
) -> list[int]:
    """批量登记（演示数据用），全部校验通过后在同一事务中写入。"""
    validated = [
        (validate_candidate(model, *candidate), candidate) for candidate in candidates
    ]
    now = now or _now()
    ids: list[int] = []
    _begin(conn)
    try:
        for (specimen, frequency_hz), (
            _raw_specimen,
            lab_a,
            reading_a,
            lab_b,
            reading_b,
        ) in validated:
            ids.append(
                _insert(
                    conn,
                    specimen,
                    lab_a,
                    reading_a,
                    lab_b,
                    reading_b,
                    frequency_hz,
                    now,
                )
            )
        refresh_reviews(conn, model)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return ids


def withdraw_record(
    conn: sqlite3.Connection,
    model: Model,
    record_id: int,
    *,
    now: str | None = None,
) -> None:
    """撤回一条有效共测记录（软删除，保留历史），同事务刷新复核状态。"""
    now = now or _now()
    _begin(conn)
    try:
        row = conn.execute(
            "SELECT id FROM co_tests WHERE id = ? AND active = 1", (record_id,)
        ).fetchone()
        if row is None:
            raise ValidationError(["共测记录不存在或已经撤回"])
        conn.execute(
            "UPDATE co_tests SET active = 0, withdrawn_at = ? WHERE id = ?",
            (now, record_id),
        )
        refresh_reviews(conn, model)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 连通性分析


def _adjacency(records: list[CoTestRecord]) -> dict[int, list[tuple[int, CoTestRecord]]]:
    adj: dict[int, list[tuple[int, CoTestRecord]]] = {}
    for rec in records:
        adj.setdefault(rec.lab_a, []).append((rec.lab_b, rec))
        adj.setdefault(rec.lab_b, []).append((rec.lab_a, rec))
    for edges in adj.values():
        edges.sort(key=lambda item: item[1].id)
    return adj


def _walk(
    reference_lab: int,
    records: list[CoTestRecord],
    *,
    skip_specimen: str | None = None,
) -> dict[int, tuple[int, CoTestRecord]]:
    """广度优先遍历，返回除参照外每个可达实验室的前驱 (上一实验室, 记录)。"""
    adj = _adjacency(
        [r for r in records if skip_specimen is None or r.specimen != skip_specimen]
    )
    previous: dict[int, tuple[int, CoTestRecord]] = {}
    seen = {reference_lab}
    queue: deque[int] = deque([reference_lab])
    while queue:
        current = queue.popleft()
        for neighbour, record in adj.get(current, []):
            if neighbour in seen:
                continue
            seen.add(neighbour)
            previous[neighbour] = (current, record)
            queue.append(neighbour)
    return previous


def _reachable(
    reference_lab: int,
    records: list[CoTestRecord],
    *,
    skip_specimen: str | None = None,
) -> set[int]:
    return {reference_lab} | set(
        _walk(reference_lab, records, skip_specimen=skip_specimen)
    )


def _fingerprint(
    reference_lab: int,
    frequency_hz: float,
    records: list[CoTestRecord],
    comparable: list[LabPath],
    critical: list[CriticalSpecimen],
) -> str:
    """结论指纹：可比集合 + 关键试件清单，并纳入该频点的有效记录集合。

    纳入记录集合（含试件号与两家实验室）使任一登记/撤回都能让旧页面的
    确认令牌失效；同一试件因此支撑的多段链也参与比对，
    但链的具体走向不进入指纹，避免仅路径变化时过度作废确认。
    """
    payload = {
        "reference_lab": reference_lab,
        "frequency_hz": canonical_frequency(frequency_hz),
        "records": sorted(
            [item.specimen, item.lab_a, item.lab_b, item.id] for item in records
        ),
        "comparable": sorted(item.lab_id for item in comparable),
        "critical": [
            [item.specimen, sorted(item.broken_labs)] for item in critical
        ],
    }
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def conclusion_from_records(
    model: Model,
    reference_lab: int,
    frequency_hz: float,
    records: list[CoTestRecord],
) -> FrequencyConclusion:
    """对给定频点做连通性 + 关键试件分析，并生成结论指纹。"""
    frequency_hz = canonical_frequency(frequency_hz)
    at_freq = [r for r in records if r.active and r.frequency_hz == frequency_hz]
    previous = _walk(reference_lab, at_freq)

    comparable: list[LabPath] = []
    for lab_id in sorted(previous):
        steps: list[ChainStep] = []
        current = lab_id
        while current != reference_lab:
            prior, record = previous[current]
            if record.lab_a == prior and record.lab_b == current:
                from_reading, to_reading = record.reading_a, record.reading_b
            else:
                from_reading, to_reading = record.reading_b, record.reading_a
            steps.append(
                ChainStep(
                    record_id=record.id,
                    specimen=record.specimen,
                    from_lab=prior,
                    to_lab=current,
                    from_reading=from_reading,
                    to_reading=to_reading,
                )
            )
            current = prior
        steps.reverse()
        comparable.append(LabPath(lab_id=lab_id, path=tuple(steps)))

    before = {reference_lab} | set(previous)
    critical: list[CriticalSpecimen] = []
    for specimen in sorted({r.specimen for r in at_freq}):
        after = _reachable(reference_lab, at_freq, skip_specimen=specimen)
        broken = sorted(before - after - {reference_lab})
        if broken:
            critical.append(
                CriticalSpecimen(
                    specimen=specimen,
                    broken_labs=tuple(broken),
                    record_ids=tuple(
                        r.id for r in at_freq if r.specimen == specimen
                    ),
                )
            )

    return FrequencyConclusion(
        reference_lab=reference_lab,
        frequency_hz=frequency_hz,
        comparable=tuple(comparable),
        critical=tuple(critical),
        fingerprint=_fingerprint(
            reference_lab, frequency_hz, at_freq, comparable, critical
        ),
    )


def conclusion(
    conn: sqlite3.Connection,
    model: Model,
    reference_lab: int,
    frequency_hz: float,
) -> FrequencyConclusion:
    frequency_hz = canonical_frequency(frequency_hz)
    records = conn.execute(
        "SELECT * FROM co_tests WHERE active = 1 AND frequency_hz = ?",
        (frequency_hz,),
    ).fetchall()
    return conclusion_from_records(
        model,
        reference_lab,
        frequency_hz,
        [_row_to_record(r) for r in records],
    )


def refresh_reviews(conn: sqlite3.Connection, model: Model) -> None:
    """重算已确认结论的指纹；变了的删除确认记录（回到待复核），其余保留。

    必须在写事务内调用；本函数只删除失效确认，不改变仍有效的确认。
    """
    rows = conn.execute(
        "SELECT reference_lab, frequency_hz, fingerprint FROM review_state"
    ).fetchall()
    for row in rows:
        current = conclusion(
            conn, model, int(row["reference_lab"]), float(row["frequency_hz"])
        )
        if current.fingerprint != row["fingerprint"]:
            conn.execute(
                "DELETE FROM review_state WHERE reference_lab = ? AND frequency_hz = ?",
                (row["reference_lab"], row["frequency_hz"]),
            )


def confirm_conclusion(
    conn: sqlite3.Connection,
    model: Model,
    reference_lab: int,
    frequency_hz: float,
    token: str,
    *,
    now: str | None = None,
) -> FrequencyConclusion:
    """确认页面所见的结论；页面令牌与当前结论不一致则拒绝且不写入。"""
    frequency_hz = canonical_frequency(frequency_hz)
    now = now or _now()
    _begin(conn)
    try:
        current = conclusion(conn, model, reference_lab, frequency_hz)
        if not token or current.fingerprint != token:
            raise StaleConclusion(current)
        conn.execute(
            """INSERT INTO review_state
               (reference_lab, frequency_hz, fingerprint, confirmed_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(reference_lab, frequency_hz) DO UPDATE SET
                 fingerprint = excluded.fingerprint,
                 confirmed_at = excluded.confirmed_at""",
            (reference_lab, frequency_hz, current.fingerprint, now),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return current


def review_states(conn: sqlite3.Connection) -> dict[tuple[int, float], dict]:
    return {
        (int(row["reference_lab"]), float(row["frequency_hz"])): dict(row)
        for row in conn.execute("SELECT * FROM review_state")
    }


# ---------------------------------------------------------------- 页面辅助


def default_reference(conn: sqlite3.Connection, model: Model) -> int | None:
    labs = {
        lab
        for row in conn.execute(
            "SELECT lab_a, lab_b FROM co_tests WHERE active = 1"
        )
        for lab in (row["lab_a"], row["lab_b"])
    }
    if labs:
        return min(labs)
    return min(model.groups) if model.groups else None


def resolve_reference(
    conn: sqlite3.Connection, model: Model, requested: object
) -> int | None:
    try:
        value = int(requested) if requested not in (None, "") else None
    except (TypeError, ValueError):
        value = None
    if value is not None and value in model.groups:
        return value
    return default_reference(conn, model)


def lab_name(model: Model, lab_id: int) -> str:
    group = model.groups.get(lab_id)
    return f"#{lab_id}「{group.get('name', '?')}」" if group else f"#{lab_id}（不存在）"


def reading_label(model: Model, reading_id: int) -> str:
    reading = model.readings.get(reading_id)
    if reading is None:
        return f"#{reading_id}（不存在）"
    freq = reading.get("frequency_hz")
    kind = reading.get("kind") or "读数"
    freq_text = f"{float(freq):g} Hz" if freq is not None else "无频率"
    return f"#{reading_id}（{kind}，{freq_text}）"


def lab_options(model: Model) -> list[dict]:
    """供页面选择实验室及查阅各实验室读数的数据。"""
    labs: list[dict] = []
    for lab_id in sorted(model.groups):
        readings = []
        for test in sorted(model.tests.values(), key=lambda t: t["id"]):
            if test["group_id"] != lab_id:
                continue
            for reading_id in model.readings_by_test.get(test["id"], []):
                reading = model.readings[reading_id]
                readings.append(
                    {
                        "id": reading_id,
                        "test": test.get("name", "?"),
                        "kind": reading.get("kind") or "",
                        "frequency_hz": reading.get("frequency_hz"),
                        "level_db": reading.get("level_db"),
                    }
                )
        readings.sort(
            key=lambda r: (
                r["frequency_hz"] is None,
                r["frequency_hz"] or 0,
                r["id"],
            )
        )
        labs.append(
            {
                "id": lab_id,
                "name": lab_name(model, lab_id),
                "readings": readings,
            }
        )
    return labs


def record_view(model: Model, record: CoTestRecord) -> dict:
    return {
        "id": record.id,
        "specimen": record.specimen,
        "frequency_hz": record.frequency_hz,
        "frequency_label": f"{record.frequency_hz:g}",
        "lab_a": record.lab_a,
        "lab_a_name": lab_name(model, record.lab_a),
        "reading_a": record.reading_a,
        "reading_a_label": reading_label(model, record.reading_a),
        "lab_b": record.lab_b,
        "lab_b_name": lab_name(model, record.lab_b),
        "reading_b": record.reading_b,
        "reading_b_label": reading_label(model, record.reading_b),
        "created_at": record.created_at,
        "withdrawn_at": record.withdrawn_at,
    }


def build_page(
    conn: sqlite3.Connection,
    model: Model,
    reference_lab: int | None,
) -> dict:
    """组装共测链页面所需的全部展示数据。"""
    active = list_records(conn)
    withdrawn = [r for r in list_records(conn, include_withdrawn=True) if not r.active]
    reviews = review_states(conn)
    frequencies = sorted({r.frequency_hz for r in active})

    views: list[dict] = []
    if reference_lab is not None:
        for frequency_hz in frequencies:
            current = conclusion_from_records(
                model, reference_lab, frequency_hz, active
            )
            paths = {item.lab_id: item for item in current.comparable}
            lab_rows = []
            for lab_id in sorted(model.groups):
                if lab_id == reference_lab:
                    continue
                lab_path = paths.get(lab_id)
                steps = []
                if lab_path:
                    for step in lab_path.path:
                        steps.append(
                            {
                                "specimen": step.specimen,
                                "from_lab_name": lab_name(model, step.from_lab),
                                "to_lab_name": lab_name(model, step.to_lab),
                                "from_reading": step.from_reading,
                                "to_reading": step.to_reading,
                            }
                        )
                lab_rows.append(
                    {
                        "lab_id": lab_id,
                        "lab_name": lab_name(model, lab_id),
                        "comparable": lab_path is not None,
                        "path": steps,
                    }
                )
            review = reviews.get((reference_lab, frequency_hz))
            confirmed = bool(
                review and review["fingerprint"] == current.fingerprint
            )
            views.append(
                {
                    "frequency_hz": current.frequency_hz,
                    "frequency_label": f"{current.frequency_hz:g}",
                    "frequency_raw": f"{current.frequency_hz:.12g}",
                    "token": current.fingerprint,
                    "record_count": sum(
                        1 for r in active if r.frequency_hz == frequency_hz
                    ),
                    "labs": lab_rows,
                    "comparable_count": len(current.comparable),
                    "critical": [
                        {
                            "specimen": item.specimen,
                            "broken_lab_names": [
                                lab_name(model, lab) for lab in item.broken_labs
                            ],
                            "record_ids": list(item.record_ids),
                        }
                        for item in current.critical
                    ],
                    "confirmed": confirmed,
                    "confirmed_at": review["confirmed_at"] if confirmed else None,
                }
            )

    return {
        "reference_lab": reference_lab,
        "reference_name": lab_name(model, reference_lab)
        if reference_lab is not None
        else None,
        "labs": lab_options(model),
        "active_records": [record_view(model, r) for r in active],
        "withdrawn_records": [record_view(model, r) for r in withdrawn],
        "views": views,
        "can_seed_demo": not active and demo_available(model),
    }


# ---------------------------------------------------------------- 演示数据

# 同一试件 S-墙体-01 在 100 Hz 同时支撑 A—B、B—C 两段共测链；
# 另一试件 S-墙体-02 提供 A—C 的直连冗余，因此 100 Hz 下撤掉 S-墙体-01
# 只会让 B 断链（C 仍可直连），而 125 Hz 下撤掉它会同时断开 B、C。
DEMO_CANDIDATES: list[tuple[str, int, int, int, int]] = [
    ("S-墙体-01", 1, 101, 2, 201),  # 100 Hz，A—B
    ("S-墙体-01", 2, 201, 3, 901),  # 100 Hz，B—C（同一试件第二段）
    ("S-墙体-02", 1, 101, 3, 901),  # 100 Hz，A—C 冗余直连
    ("S-墙体-01", 1, 102, 2, 202),  # 125 Hz，A—B
    ("S-墙体-01", 2, 202, 3, 902),  # 125 Hz，B—C（同一试件第二段）
]


def demo_available(model: Model) -> bool:
    try:
        for candidate in DEMO_CANDIDATES:
            validate_candidate(model, *candidate)
    except (ValidationError, KeyError, TypeError, ValueError):
        return False
    return True


def seed_demo(
    conn: sqlite3.Connection,
    model: Model,
    *,
    now: str | None = None,
) -> list[int]:
    if list_records(conn):
        raise ValidationError(["当前已有有效共测记录，未载入演示数据"])
    if not demo_available(model):
        raise ValidationError(
            ["当前库与演示数据不匹配（需使用 scripts/make_sample_db.py 生成的演示库）"]
        )
    return add_records(conn, model, DEMO_CANDIDATES, now=now)
