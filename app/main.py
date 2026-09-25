"""实验室混样批次追溯 API。

- POST /samples                        登记原始样本
- POST /exclusion-rules                登记原始样本禁配对（无序对，重复登记幂等）
- GET  /exclusion-rules                列出全部禁配规则
- POST /exclusion-rules/impact-preview 禁配补录影响预演：两样本首次合流批次与受影响批次（只读，不登记规则）
- POST /pools                          创建混样批次（引用已有批次 + 正整数份数，命中禁配返回 409）
- POST /pool-plans                     按目标占比反算整数份数配方（只规划，不创建批次）
- GET  /batches                        列出全部批次
- GET  /batches/{batch_id}             批次详情（混样含配方）
- GET  /batches/{batch_id}/composition 递归计算原始样本占比（约分后的分子/分母）
- POST /batches/{batch_id}/recipe-preview 配方变更影响预演（内存计算，不写库）
- POST /batches/{batch_id}/portion-diagnosis 份数诊断：假设仅一项份数记错，反推正整数份数（不写库）
- POST /batches/{batch_id}/inventory   为原始样本补入正整数份库存（累加）
- GET  /batches/{batch_id}/inventory   查询原始样本库存余额
- POST /batches/{batch_id}/preparations 批次制备：正整数次数，沿配方图整数消耗原始样本库存（不足 409 且整体回滚）
- POST /preparation-jobs               整单制备：多批次清单合并核算，按样本汇总需求一次扣减（不足 409 且整单回滚）
- POST /preparation-jobs/selection-preview 只读排产预演：1～12 条候选制备单按同一库存快照选型（不扣库存、不写记录）
- GET  /preparations/{preparation_id}  按记录 id 查询制备记录（含消耗明细与当前剩余库存）
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from fractions import Fraction

from fastapi import Depends, FastAPI, HTTPException, Response, status

from . import db
from .schemas import (
    AffectedBatchOut,
    BatchOut,
    ComponentOut,
    CompositionOut,
    ExclusionRuleCreate,
    ExclusionRuleOut,
    FractionOut,
    ImpactPreviewBatchOut,
    ImpactPreviewOut,
    ImpactPreviewRequest,
    InventoryAddRequest,
    InventoryOut,
    PoolCreate,
    PoolPlanOut,
    PoolPlanRecipeItemOut,
    PoolPlanRequest,
    PortionDiagnosisOut,
    PortionDiagnosisRequest,
    PortionDiagnosisSolutionOut,
    PortionDiagnosisUndeterminedOut,
    PreparationConsumptionOut,
    PreparationCreateRequest,
    PreparationJobBatchOut,
    PreparationJobCreateRequest,
    PreparationJobOut,
    PreparationOut,
    PreviewComponentOut,
    RecipeItemOut,
    RecipePreviewOut,
    RecipePreviewRequest,
    SampleCreate,
    SelectionPreviewOut,
    SelectionPreviewRequest,
)
from .service import (
    PoolPlanError,
    batch_composition,
    exclusion_impact_preview,
    pool_plan,
    portion_diagnosis,
    preparation_job_requirements,
    preparation_requirements,
    reachable_samples,
    recipe_preview,
    select_preparation_subset,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()  # 启动时自动建表
    yield


app = FastAPI(
    title="实验室混样批次追溯 API",
    version="1.0.0",
    lifespan=lifespan,
)


def get_db() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def _fetch_batch(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()


def _batch_out(conn: sqlite3.Connection, row: sqlite3.Row) -> BatchOut:
    recipe = None
    if row["kind"] == "pool":
        recipe = [
            RecipeItemOut(
                batch_id=r["ingredient_id"],
                batch_name=r["name"],
                portions=r["portions"],
            )
            for r in conn.execute(
                """
                SELECT pr.ingredient_id, b.name, pr.portions
                FROM pool_recipes pr
                JOIN batches b ON b.id = pr.ingredient_id
                WHERE pr.pool_id = ?
                ORDER BY pr.ingredient_id
                """,
                (row["id"],),
            )
        ]
    return BatchOut(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        created_at=row["created_at"],
        recipe=recipe,
    )


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/samples",
    response_model=BatchOut,
    status_code=status.HTTP_201_CREATED,
    summary="登记原始样本",
)
def create_sample(payload: SampleCreate, conn: sqlite3.Connection = Depends(get_db)) -> BatchOut:
    try:
        cur = conn.execute(
            "INSERT INTO batches (name, kind) VALUES (?, 'sample')", (payload.name,)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"批次名称已存在: {payload.name}"
        ) from None
    return _batch_out(conn, _fetch_batch(conn, cur.lastrowid))


@app.get(
    "/exclusion-rules",
    response_model=list[ExclusionRuleOut],
    summary="列出全部原始样本禁配规则",
)
def list_exclusion_rules(conn: sqlite3.Connection = Depends(get_db)) -> list[ExclusionRuleOut]:
    rows = conn.execute(
        """
        SELECT low_sample_id, high_sample_id, created_at
        FROM exclusion_rules
        ORDER BY low_sample_id, high_sample_id
        """
    ).fetchall()
    return [
        ExclusionRuleOut(
            low_sample_id=r["low_sample_id"],
            high_sample_id=r["high_sample_id"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


@app.post(
    "/exclusion-rules",
    response_model=ExclusionRuleOut,
    status_code=status.HTTP_201_CREATED,
    summary="登记原始样本禁配对",
)
def create_exclusion_rule(
    payload: ExclusionRuleCreate,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
) -> ExclusionRuleOut:
    low_id, high_id = sorted((payload.sample_a_id, payload.sample_b_id))
    if low_id == high_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="两个原始样本 id 必须不同")

    placeholders = ", ".join("?" for _ in (low_id, high_id))
    rows = {
        r["id"]: r
        for r in conn.execute(
            f"SELECT id, kind FROM batches WHERE id IN ({placeholders})", (low_id, high_id)
        )
    }
    missing = [i for i in (low_id, high_id) if i not in rows]
    if missing:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"未知批次 id: {missing}")
    not_samples = [
        i for i in (low_id, high_id) if rows[i]["kind"] != "sample"
    ]
    if not_samples:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"禁配对象必须是原始样本（kind='sample'）: {not_samples}",
        )

    # 与混样创建共用写事务（BEGIN IMMEDIATE 立即获取写锁）：
    # 规则登记与 /pools 的禁配检查相互串行，不会出现新规则提交后漏检
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT created_at FROM exclusion_rules WHERE low_sample_id = ? AND high_sample_id = ?",
            (low_id, high_id),
        ).fetchone()
        if existing is not None:
            # 同一无序对重复登记：不生成第二条规则，幂等返回 200
            conn.rollback()
            response.status_code = status.HTTP_200_OK
            return ExclusionRuleOut(
                low_sample_id=low_id,
                high_sample_id=high_id,
                created_at=existing["created_at"],
            )
        cur = conn.execute(
            "INSERT INTO exclusion_rules (low_sample_id, high_sample_id) VALUES (?, ?)",
            (low_id, high_id),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # 并发下另一事务抢先登记同一对：与重复登记同等处理
        conn.rollback()
        row = conn.execute(
            "SELECT created_at FROM exclusion_rules WHERE low_sample_id = ? AND high_sample_id = ?",
            (low_id, high_id),
        ).fetchone()
        response.status_code = status.HTTP_200_OK
        return ExclusionRuleOut(
            low_sample_id=low_id,
            high_sample_id=high_id,
            created_at=row["created_at"],
        )
    row = conn.execute(
        "SELECT created_at FROM exclusion_rules WHERE rowid = ?", (cur.lastrowid,)
    ).fetchone()
    return ExclusionRuleOut(
        low_sample_id=low_id,
        high_sample_id=high_id,
        created_at=row["created_at"],
    )


@app.post(
    "/exclusion-rules/impact-preview",
    response_model=ImpactPreviewOut,
    summary="禁配补录影响预演（只读：不登记规则、不改配方）",
)
def preview_exclusion_impact(
    payload: ImpactPreviewRequest, conn: sqlite3.Connection = Depends(get_db)
) -> ImpactPreviewOut:
    low_id, high_id = sorted((payload.sample_a_id, payload.sample_b_id))
    if low_id == high_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="两个原始样本 id 必须不同")

    # 校验沿用登记禁配对的规则：输入不存在或非原始样本均报 400；
    # 但允许该无序对尚未登记为禁配对——预演回答的是“若补录会影响哪些批次”
    placeholders = ", ".join("?" for _ in (low_id, high_id))
    rows = {
        r["id"]: r
        for r in conn.execute(
            f"SELECT id, kind FROM batches WHERE id IN ({placeholders})", (low_id, high_id)
        )
    }
    missing = [i for i in (low_id, high_id) if i not in rows]
    if missing:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"未知批次 id: {missing}")
    not_samples = [i for i in (low_id, high_id) if rows[i]["kind"] != "sample"]
    if not_samples:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"预演对象必须是原始样本（kind='sample'）: {not_samples}",
        )

    low_id, high_id, affected = exclusion_impact_preview(conn, low_id, high_id)
    names = {
        r["id"]: r["name"]
        for r in conn.execute(
            "SELECT id, name FROM batches WHERE id IN (%s)"
            % ", ".join("?" for _ in affected),
            [batch_id for batch_id, _, _ in affected],
        )
    } if affected else {}

    return ImpactPreviewOut(
        low_sample_id=low_id,
        high_sample_id=high_id,
        affected_batches=[
            ImpactPreviewBatchOut(
                batch_id=batch_id,
                batch_name=names[batch_id],
                is_first_confluence=is_first,
                first_confluence_batch_ids=trace,
            )
            for batch_id, is_first, trace in affected
        ],
    )


@app.post(
    "/pools",
    response_model=BatchOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建混样批次",
)
def create_pool(payload: PoolCreate, conn: sqlite3.Connection = Depends(get_db)) -> BatchOut:
    # 同一批次在配方中重复出现时，份数合并
    merged: dict[int, int] = {}
    for item in payload.recipe:
        merged[item.batch_id] = merged.get(item.batch_id, 0) + item.portions

    ids = sorted(merged)
    placeholders = ", ".join("?" for _ in ids)
    found = {
        row["id"]
        for row in conn.execute(f"SELECT id FROM batches WHERE id IN ({placeholders})", ids)
    }
    missing = [i for i in ids if i not in found]
    if missing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"未知批次 id: {missing}"
        )

    # 立即获取写锁后再做禁配检查与插入：并发登记的禁配规则若已提交，
    # 必然在本事务的快照中可见（或其写锁迫使本事务等待其提交），不会漏检
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 沿配方图递归求新混样可达的全部原始样本（含嵌套混样与汇合路径）
        sample_ids = reachable_samples(conn, ids)
        conflicting = conn.execute(
            f"""
            SELECT low_sample_id, high_sample_id
            FROM exclusion_rules
            WHERE low_sample_id IN ({", ".join("?" for _ in sample_ids)})
              AND high_sample_id IN ({", ".join("?" for _ in sample_ids)})
            ORDER BY low_sample_id, high_sample_id
            LIMIT 1
            """,
            (*sorted(sample_ids), *sorted(sample_ids)),
        ).fetchone() if sample_ids else None
        if conflicting is not None:
            conn.rollback()
            low_id, high_id = conflicting["low_sample_id"], conflicting["high_sample_id"]
            names = {
                r["id"]: r["name"]
                for r in conn.execute(
                    "SELECT id, name FROM batches WHERE id IN (?, ?)", (low_id, high_id)
                )
            }
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "message": "混样命中原始样本禁配规则",
                    "pair": [
                        {"sample_id": low_id, "sample_name": names[low_id]},
                        {"sample_id": high_id, "sample_name": names[high_id]},
                    ],
                },
            )

        cur = conn.execute(
            "INSERT INTO batches (name, kind) VALUES (?, 'pool')", (payload.name,)
        )
        pool_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO pool_recipes (pool_id, ingredient_id, portions) VALUES (?, ?, ?)",
            [(pool_id, i, merged[i]) for i in ids],
        )
        conn.commit()
    except HTTPException:
        raise
    except sqlite3.IntegrityError:
        conn.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"批次名称已存在: {payload.name}"
        ) from None
    return _batch_out(conn, _fetch_batch(conn, pool_id))


@app.post(
    "/pool-plans",
    response_model=PoolPlanOut,
    summary="按目标占比规划混样配方（不创建批次）",
)
def plan_pool(
    payload: PoolPlanRequest, conn: sqlite3.Connection = Depends(get_db)
) -> PoolPlanOut:
    # 两个原料与目标样本都必须是已存在的批次；未知批次沿用 /pools 的 400 校验
    ids = [payload.ingredient_a_batch_id, payload.ingredient_b_batch_id, payload.sample_id]
    placeholders = ", ".join("?" for _ in ids)
    rows = {
        row["id"]: row
        for row in conn.execute(
            f"SELECT id, name, kind FROM batches WHERE id IN ({placeholders})", ids
        )
    }
    missing = [i for i in ids if i not in rows]
    if missing:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"未知批次 id: {missing}")
    if rows[payload.sample_id]["kind"] != "sample":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"目标样本必须是原始样本（kind='sample'）: {payload.sample_id}",
        )

    target = Fraction(payload.numerator, payload.denominator)
    try:
        portions_a, portions_b, composition = pool_plan(
            conn,
            payload.ingredient_a_batch_id,
            payload.ingredient_b_batch_id,
            payload.sample_id,
            target,
            payload.max_total_portions,
        )
    except PoolPlanError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None

    # 新混样涉及的原始样本名称一次性解析
    sample_ids = sorted(composition)
    placeholders = ", ".join("?" for _ in sample_ids)
    names = {
        r["id"]: r["name"]
        for r in conn.execute(
            f"SELECT id, name FROM batches WHERE id IN ({placeholders})", sample_ids
        )
    }
    components = [
        ComponentOut(
            sample_id=sid,
            sample_name=names[sid],
            numerator=composition[sid].numerator,
            denominator=composition[sid].denominator,
        )
        for sid in sample_ids
    ]

    return PoolPlanOut(
        sample_id=payload.sample_id,
        target=FractionOut(numerator=target.numerator, denominator=target.denominator),
        total_portions=portions_a + portions_b,
        recipe=[
            PoolPlanRecipeItemOut(
                batch_id=payload.ingredient_a_batch_id, portions=portions_a
            ),
            PoolPlanRecipeItemOut(
                batch_id=payload.ingredient_b_batch_id, portions=portions_b
            ),
        ],
        components=components,
    )


@app.get("/batches", response_model=list[BatchOut], summary="列出全部批次")
def list_batches(conn: sqlite3.Connection = Depends(get_db)) -> list[BatchOut]:
    rows = conn.execute("SELECT * FROM batches ORDER BY id").fetchall()
    return [_batch_out(conn, row) for row in rows]


@app.get("/batches/{batch_id}", response_model=BatchOut, summary="批次详情")
def get_batch(batch_id: int, conn: sqlite3.Connection = Depends(get_db)) -> BatchOut:
    row = _fetch_batch(conn, batch_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"批次不存在: {batch_id}")
    return _batch_out(conn, row)


@app.get(
    "/batches/{batch_id}/composition",
    response_model=CompositionOut,
    summary="递归计算原始样本占比",
)
def get_composition(
    batch_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> CompositionOut:
    row = _fetch_batch(conn, batch_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"批次不存在: {batch_id}")

    composition = batch_composition(conn, batch_id)
    names = {
        r["id"]: r["name"]
        for r in conn.execute(
            "SELECT id, name FROM batches WHERE id IN (%s)"
            % ", ".join("?" for _ in composition),
            list(composition),
        )
    }
    components = [
        ComponentOut(
            sample_id=sample_id,
            sample_name=names[sample_id],
            numerator=frac.numerator,
            denominator=frac.denominator,
        )
        for sample_id, frac in sorted(composition.items())
    ]
    return CompositionOut(
        batch_id=row["id"],
        batch_name=row["name"],
        kind=row["kind"],
        components=components,
    )


@app.post(
    "/batches/{batch_id}/recipe-preview",
    response_model=RecipePreviewOut,
    summary="配方变更影响预演（不修改数据库）",
)
def preview_recipe(
    batch_id: int,
    payload: RecipePreviewRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> RecipePreviewOut:
    row = _fetch_batch(conn, batch_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"批次不存在: {batch_id}")
    if row["kind"] != "pool":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"原始样本没有配方可替换: {batch_id}",
        )

    # 与创建混样一致：同一批次在配方中重复出现时，份数合并
    merged: dict[int, int] = {}
    for item in payload.recipe:
        merged[item.batch_id] = merged.get(item.batch_id, 0) + item.portions

    ids = sorted(merged)
    placeholders = ", ".join("?" for _ in ids)
    found = {
        r["id"]
        for r in conn.execute(f"SELECT id FROM batches WHERE id IN ({placeholders})", ids)
    }
    missing = [i for i in ids if i not in found]
    if missing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"未知批次 id: {missing}"
        )

    # 避免预演成环：原料只能引用比目标批次更早创建（id 更小）的批次
    too_late = [i for i in ids if i >= batch_id]
    if too_late:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"原料只能引用比批次 {batch_id} 更早创建的批次: {too_late}",
        )

    affected = recipe_preview(conn, batch_id, merged)

    # 一次性解析受影响批次、涉及样本与替代配方原料的名称/类型
    involved = set(affected) | set(ids)
    for original, preview in affected.values():
        involved.update(original)
        involved.update(preview)
    info = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, name, kind FROM batches WHERE id IN (%s)"
            % ", ".join("?" for _ in involved),
            sorted(involved),
        )
    }

    def frac_out(frac: Fraction) -> FractionOut:
        return FractionOut(numerator=frac.numerator, denominator=frac.denominator)

    batches_out = []
    for bid, (original, preview) in affected.items():
        components = [
            PreviewComponentOut(
                sample_id=sample_id,
                sample_name=info[sample_id]["name"],
                original=frac_out(original.get(sample_id, Fraction(0))),
                preview=frac_out(preview.get(sample_id, Fraction(0))),
                delta=frac_out(
                    preview.get(sample_id, Fraction(0))
                    - original.get(sample_id, Fraction(0))
                ),
            )
            for sample_id in sorted(set(original) | set(preview))
        ]
        batches_out.append(
            AffectedBatchOut(
                batch_id=bid,
                batch_name=info[bid]["name"],
                kind=info[bid]["kind"],
                components=components,
            )
        )

    return RecipePreviewOut(
        batch_id=row["id"],
        batch_name=row["name"],
        recipe=[
            RecipeItemOut(batch_id=i, batch_name=info[i]["name"], portions=merged[i])
            for i in ids
        ],
        affected_batches=batches_out,
    )


@app.post(
    "/batches/{batch_id}/portion-diagnosis",
    response_model=PortionDiagnosisOut,
    summary="份数诊断：假设仅一项份数记错，反推正整数份数（不修改数据库）",
)
def diagnose_portions(
    batch_id: int,
    payload: PortionDiagnosisRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> PortionDiagnosisOut:
    row = _fetch_batch(conn, batch_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"批次不存在: {batch_id}")
    if row["kind"] != "pool":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"目标批次必须是混样批次（kind='pool'）: {batch_id}",
        )

    # 实测对象必须都是已存在的原始样本（重复 id、占比之和已在请求模型中校验）
    sample_ids = [m.sample_id for m in payload.measurements]
    if sample_ids:
        placeholders = ", ".join("?" for _ in sample_ids)
        rows = {
            r["id"]: r
            for r in conn.execute(
                f"SELECT id, kind FROM batches WHERE id IN ({placeholders})", sample_ids
            )
        }
        missing = [i for i in sample_ids if i not in rows]
        if missing:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail=f"未知批次 id: {missing}"
            )
        not_samples = [i for i in sample_ids if rows[i]["kind"] != "sample"]
        if not_samples:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"实测对象必须是原始样本（kind='sample'）: {not_samples}",
            )

    measured = {
        m.sample_id: Fraction(m.numerator, m.denominator) for m in payload.measurements
    }
    solutions, undetermined = portion_diagnosis(
        conn, batch_id, measured, payload.max_portions
    )

    # 一次性解析解与无法判定项涉及的原料名称
    ingredient_ids = [i for i, _, _ in solutions] + [i for i, _ in undetermined]
    names = (
        {
            r["id"]: r["name"]
            for r in conn.execute(
                "SELECT id, name FROM batches WHERE id IN (%s)"
                % ", ".join("?" for _ in ingredient_ids),
                ingredient_ids,
            )
        }
        if ingredient_ids
        else {}
    )

    return PortionDiagnosisOut(
        batch_id=row["id"],
        batch_name=row["name"],
        solutions=[
            PortionDiagnosisSolutionOut(
                ingredient_batch_id=i,
                ingredient_name=names[i],
                original_portions=original,
                corrected_portions=corrected,
            )
            for i, original, corrected in solutions
        ],
        undetermined=[
            PortionDiagnosisUndeterminedOut(
                ingredient_batch_id=i,
                ingredient_name=names[i],
                original_portions=original,
            )
            for i, original in undetermined
        ],
    )


def _inventory_out(conn: sqlite3.Connection, row: sqlite3.Row) -> InventoryOut:
    """构造原始样本库存响应；row 须含 sample_id/balance/updated_at/sample_name。"""
    return InventoryOut(
        sample_id=row["sample_id"],
        sample_name=row["sample_name"],
        balance=row["balance"],
        updated_at=row["updated_at"],
    )


@app.post(
    "/batches/{batch_id}/inventory",
    response_model=InventoryOut,
    summary="为原始样本补入库存（正整数份数，累加到余额）",
)
def add_inventory(
    batch_id: int,
    payload: InventoryAddRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> InventoryOut:
    # 与 /pools 一致：未知批次按业务输入错误返回 400
    row = _fetch_batch(conn, batch_id)
    if row is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"未知批次 id: {batch_id}")
    if row["kind"] != "sample":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"只有原始样本可补入库存（kind='sample'）: {batch_id}",
        )

    conn.execute("BEGIN IMMEDIATE")
    # 同一写事务内累加余额并刷新更新时间：UPSERT 幂等兼容“首次补入/再次补入”
    conn.execute(
        """
        INSERT INTO sample_inventory (sample_id, balance, updated_at)
        VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        ON CONFLICT (sample_id) DO UPDATE SET
            balance = balance + excluded.balance,
            updated_at = excluded.updated_at
        """,
        (batch_id, payload.portions),
    )
    conn.commit()
    row = conn.execute(
        """
        SELECT si.sample_id, b.name AS sample_name, si.balance, si.updated_at
        FROM sample_inventory si
        JOIN batches b ON b.id = si.sample_id
        WHERE si.sample_id = ?
        """,
        (batch_id,),
    ).fetchone()
    return _inventory_out(conn, row)


@app.get(
    "/batches/{batch_id}/inventory",
    response_model=InventoryOut,
    summary="查询原始样本库存余额",
)
def get_inventory(
    batch_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> InventoryOut:
    row = _fetch_batch(conn, batch_id)
    if row is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"未知批次 id: {batch_id}")
    if row["kind"] != "sample":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"只有原始样本有库存（kind='sample'）: {batch_id}",
        )

    inv = conn.execute(
        """
        SELECT si.sample_id, b.name AS sample_name, si.balance, si.updated_at
        FROM sample_inventory si
        JOIN batches b ON b.id = si.sample_id
        WHERE si.sample_id = ?
        """,
        (batch_id,),
    ).fetchone()
    # 从未补入过的原始样本余额视为 0
    if inv is None:
        inv = {
            "sample_id": batch_id,
            "sample_name": row["name"],
            "balance": 0,
            "updated_at": None,
        }
    return _inventory_out(conn, inv)


def _build_preparation_out(
    conn: sqlite3.Connection, prep: sqlite3.Row
) -> PreparationOut:
    """组装制备记录响应：消耗明细 + 当前剩余库存（实时查询，GET 复用）。"""
    # LEFT JOIN：每个消耗样本对应一行剩余库存；正常数据下被消耗样本必有库存行，
    # COALESCE 仅为异常数据兜底（余额视为 0、无更新时间）
    rows = conn.execute(
        """
        SELECT pc.sample_id, b.name AS sample_name, pc.portions,
               COALESCE(si.balance, 0) AS balance, si.updated_at AS updated_at
        FROM preparation_consumptions pc
        JOIN batches b ON b.id = pc.sample_id
        LEFT JOIN sample_inventory si ON si.sample_id = pc.sample_id
        WHERE pc.preparation_id = ?
        ORDER BY pc.sample_id
        """,
        (prep["id"],),
    ).fetchall()
    batch_name = conn.execute(
        "SELECT name FROM batches WHERE id = ?", (prep["batch_id"],)
    ).fetchone()["name"]
    return PreparationOut(
        id=prep["id"],
        batch_id=prep["batch_id"],
        batch_name=batch_name,
        times=prep["times"],
        created_at=prep["created_at"],
        consumptions=[
            PreparationConsumptionOut(
                sample_id=r["sample_id"], sample_name=r["sample_name"], portions=r["portions"]
            )
            for r in rows
        ],
        remaining_inventory=[
            InventoryOut(
                sample_id=r["sample_id"],
                sample_name=r["sample_name"],
                balance=r["balance"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ],
    )


def _ensure_inventory(conn: sqlite3.Connection, required: dict[int, int]) -> None:
    """在同一写事务内核对各原始样本余额是否满足（单次/整单）汇总需求。

    任一样本不足即回滚当前事务并抛 409：detail.shortages 按样本 id 升序给出
    每个缺口样本的总需求、余额与缺口；全部足量时不做任何修改、直接返回。
    """
    sample_ids = sorted(required)
    balances = (
        {
            r["sample_id"]: r["balance"]
            for r in conn.execute(
                "SELECT sample_id, balance FROM sample_inventory WHERE sample_id IN (%s)"
                % ", ".join("?" for _ in sample_ids),
                sample_ids,
            )
        }
        if sample_ids
        else {}
    )

    shortages = []
    for sid in sample_ids:
        available = balances.get(sid, 0)
        if available < required[sid]:
            shortages.append({
                "sample_id": sid,
                "required": required[sid],
                "available": available,
                "shortage": required[sid] - available,
            })
    if not shortages:
        return
    shortage_ids = [item["sample_id"] for item in shortages]
    names = {
        r["id"]: r["name"]
        for r in conn.execute(
            "SELECT id, name FROM batches WHERE id IN (%s)"
            % ", ".join("?" for _ in shortage_ids),
            shortage_ids,
        )
    }
    for item in shortages:
        item["sample_name"] = names[item["sample_id"]]
    conn.rollback()
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        detail={"message": "原始样本库存不足", "shortages": shortages},
    )


@app.post(
    "/batches/{batch_id}/preparations",
    response_model=PreparationOut,
    status_code=status.HTTP_201_CREATED,
    summary="批次制备：按配方整数消耗原始样本库存（同一事务核算与扣减）",
)
def create_preparation(
    batch_id: int,
    payload: PreparationCreateRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> PreparationOut:
    # 未知批次按需求明确返回 400（区别于 GET /batches/{id} 的 404）
    row = _fetch_batch(conn, batch_id)
    if row is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"未知批次 id: {batch_id}")

    # 核算与扣减必须在同一 SQLite 写事务内完成：BEGIN IMMEDIATE 取写锁，
    # 余额读取、缺口核算、库存扣减与制备记录写入彼此原子，并发制备相互串行
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 沿配方图逐层乘算、多路径合并，得到各原始样本的整数需求；
        # 原始样本目标直接消耗自身（每制备一次 1 份）
        required = preparation_requirements(conn, batch_id, payload.times)
        sample_ids = sorted(required)

        # 任一样本库存不足即整体回滚：库存与制备记录均不变化
        _ensure_inventory(conn, required)

        # 足量：一次扣减并写入制备记录与消耗明细（同一事务提交）
        cur = conn.execute(
            "INSERT INTO preparations (batch_id, times) VALUES (?, ?)",
            (batch_id, payload.times),
        )
        preparation_id = cur.lastrowid
        conn.executemany(
            """
            INSERT INTO preparation_consumptions (preparation_id, sample_id, portions)
            VALUES (?, ?, ?)
            """,
            [(preparation_id, sid, required[sid]) for sid in sample_ids],
        )
        conn.executemany(
            "UPDATE sample_inventory SET balance = balance - ? WHERE sample_id = ?",
            [(required[sid], sid) for sid in sample_ids],
        )
        conn.commit()
    except HTTPException:
        raise

    prep = conn.execute(
        "SELECT * FROM preparations WHERE id = ?", (preparation_id,)
    ).fetchone()
    return _build_preparation_out(conn, prep)


@app.get(
    "/preparations/{preparation_id}",
    response_model=PreparationOut,
    summary="按记录 id 查询制备记录（含消耗明细与当前剩余库存）",
)
def get_preparation(
    preparation_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> PreparationOut:
    prep = conn.execute(
        "SELECT * FROM preparations WHERE id = ?", (preparation_id,)
    ).fetchone()
    if prep is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"制备记录不存在: {preparation_id}"
        )
    return _build_preparation_out(conn, prep)


@app.post(
    "/preparation-jobs",
    response_model=PreparationJobOut,
    status_code=status.HTTP_201_CREATED,
    summary="整单制备：多批次清单合并核算，按样本汇总需求一次扣减（不足 409 且整单回滚）",
)
def create_preparation_job(
    payload: PreparationJobCreateRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> PreparationJobOut:
    # 同一批次在清单中重复出现时，先合并制备次数（与 /pools 合并重复原料一致）
    merged: dict[int, int] = {}
    for item in payload.items:
        merged[item.batch_id] = merged.get(item.batch_id, 0) + item.times

    ids = sorted(merged)
    placeholders = ", ".join("?" for _ in ids)
    rows = {
        row["id"]: row
        for row in conn.execute(
            f"SELECT id, name FROM batches WHERE id IN ({placeholders})", ids
        )
    }
    missing = [i for i in ids if i not in rows]
    if missing:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"未知批次 id: {missing}")

    # 与单批次制备同一事务模型：余额读取、缺口核算、库存扣减与各批次制备记录
    # 写入都在同一 SQLite 写事务内完成（BEGIN IMMEDIATE 立即取写锁）
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 沿配方图逐层乘算每项的整数需求，再按样本汇总整单需求：
        # 共享样本按汇总后的总需求核算，不能让各项分别判为足量
        per_batch = preparation_job_requirements(conn, [(i, merged[i]) for i in ids])
        total: dict[int, int] = {}
        for batch_id in ids:
            for sample_id, portions in per_batch[batch_id].items():
                total[sample_id] = total.get(sample_id, 0) + portions

        # 任一样本不足：整单回滚，不留任何制备记录、余额不变
        _ensure_inventory(conn, total)

        # 全部足量：每个批次写入一条制备记录与消耗明细，整单按汇总需求一次扣减
        preparation_ids: dict[int, int] = {}
        for batch_id in ids:
            cur = conn.execute(
                "INSERT INTO preparations (batch_id, times) VALUES (?, ?)",
                (batch_id, merged[batch_id]),
            )
            preparation_ids[batch_id] = cur.lastrowid
            conn.executemany(
                """
                INSERT INTO preparation_consumptions (preparation_id, sample_id, portions)
                VALUES (?, ?, ?)
                """,
                [
                    (preparation_ids[batch_id], sid, per_batch[batch_id][sid])
                    for sid in sorted(per_batch[batch_id])
                ],
            )
        conn.executemany(
            "UPDATE sample_inventory SET balance = balance - ? WHERE sample_id = ?",
            [(total[sid], sid) for sid in sorted(total)],
        )
        conn.commit()
    except HTTPException:
        raise

    # 响应中的样本名称一次性解析（消耗过的样本必然存在）
    sample_ids = sorted(total)
    sample_names = {
        r["id"]: r["name"]
        for r in conn.execute(
            "SELECT id, name FROM batches WHERE id IN (%s)"
            % ", ".join("?" for _ in sample_ids),
            sample_ids,
        )
    }

    def consumption_out(sample_id: int, portions: int) -> PreparationConsumptionOut:
        return PreparationConsumptionOut(
            sample_id=sample_id,
            sample_name=sample_names[sample_id],
            portions=portions,
        )

    return PreparationJobOut(
        preparations=[
            PreparationJobBatchOut(
                batch_id=batch_id,
                batch_name=rows[batch_id]["name"],
                preparation_id=preparation_ids[batch_id],
                times=merged[batch_id],
                consumptions=[
                    consumption_out(sid, per_batch[batch_id][sid])
                    for sid in sorted(per_batch[batch_id])
                ],
            )
            for batch_id in ids
        ],
        consumptions=[consumption_out(sid, total[sid]) for sid in sample_ids],
    )


@app.post(
    "/preparation-jobs/selection-preview",
    response_model=SelectionPreviewOut,
    summary="只读排产预演：按同一库存快照为候选制备单选型（不扣库存、不写记录）",
)
def preview_preparation_job_selection(
    payload: SelectionPreviewRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> SelectionPreviewOut:
    # 订单按请求位置（从 1 开始）标识：同一批次重复出现时不合并次数，
    # 各自作为独立候选订单参与选型
    items = payload.items

    # 未知批次沿用 /preparation-jobs 的约定：按业务输入错误返回 400
    ids = sorted({item.batch_id for item in items})
    placeholders = ", ".join("?" for _ in ids)
    found = {
        row["id"]
        for row in conn.execute(f"SELECT id FROM batches WHERE id IN ({placeholders})", ids)
    }
    missing = [i for i in ids if i not in found]
    if missing:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"未知批次 id: {missing}")

    # 显式只读事务：需求展开、余额读取与样本名称解析都落在同一快照中，
    # 并发补库/制备不会在预演中途混入；纯 SELECT 不阻塞其他写事务
    conn.execute("BEGIN")
    try:
        # 复用配方图整数消耗展开：每条候选订单按其批次与次数得到各原始样本需求
        demands = [
            preparation_requirements(conn, item.batch_id, item.times) for item in items
        ]
        balances = {
            r["sample_id"]: r["balance"]
            for r in conn.execute("SELECT sample_id, balance FROM sample_inventory")
        }

        selected, unselected = select_preparation_subset(
            demands, [item.times for item in items], balances
        )

        # 入选订单按样本汇总的总消耗
        consumed: dict[int, int] = {}
        for position in selected:
            for sample_id, portions in demands[position - 1].items():
                consumed[sample_id] = consumed.get(sample_id, 0) + portions

        sample_ids = sorted(consumed)
        rows = (
            conn.execute(
                """
                SELECT b.id AS sample_id, b.name AS sample_name,
                       COALESCE(si.balance, 0) AS balance, si.updated_at AS updated_at
                FROM batches b
                LEFT JOIN sample_inventory si ON si.sample_id = b.id
                WHERE b.id IN (%s)
                ORDER BY b.id
                """
                % ", ".join("?" for _ in sample_ids),
                sample_ids,
            ).fetchall()
            if sample_ids
            else []
        )
        info = {r["sample_id"]: r for r in rows}
    finally:
        conn.rollback()  # 只读预演：任何路径都不落盘

    return SelectionPreviewOut(
        selected_positions=selected,
        unselected_positions=unselected,
        consumptions=[
            PreparationConsumptionOut(
                sample_id=sid,
                sample_name=info[sid]["sample_name"],
                portions=consumed[sid],
            )
            for sid in sample_ids
        ],
        remaining_inventory=[
            InventoryOut(
                sample_id=sid,
                sample_name=info[sid]["sample_name"],
                balance=info[sid]["balance"] - consumed[sid],
                updated_at=info[sid]["updated_at"],
            )
            for sid in sample_ids
        ],
    )
