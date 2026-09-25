"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from fractions import Fraction
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

# 严格的正整数：拒绝 0、负数、浮点数（如 1.5 / 2.0）与字符串（如 "3"）
StrictPositiveInt = Annotated[int, Field(strict=True, gt=0)]

# 严格的非负整数：允许 0，其余规则同 StrictPositiveInt
StrictNonNegativeInt = Annotated[int, Field(strict=True, ge=0)]

BatchName = Annotated[str, Field(min_length=1, max_length=200)]


class SampleCreate(BaseModel):
    name: BatchName


class ExclusionRuleCreate(BaseModel):
    """登记一对不同的原始样本禁配对；顺序无关，服务端按 id 归一化。"""

    sample_a_id: StrictPositiveInt
    sample_b_id: StrictPositiveInt


class ExclusionRuleOut(BaseModel):
    # 无序对归一后按 id 升序返回
    low_sample_id: int
    high_sample_id: int
    created_at: str


class ImpactPreviewRequest(BaseModel):
    """禁配补录影响预演：一对不同的原始样本 id。

    允许该无序对尚未登记为禁配规则；顺序无关，服务端按 id 归一化。预演只读。
    """

    sample_a_id: StrictPositiveInt
    sample_b_id: StrictPositiveInt


class ImpactPreviewBatchOut(BaseModel):
    batch_id: int
    batch_name: BatchName
    # 首次合流：自身同时含两样本，但每个直接原料都不同时含两样本
    is_first_confluence: bool
    # 沿配方 DAG 可追溯到的全部首次合流批次 id（自身为首次合流时含自身）；
    # 嵌套与多路径汇合不去重不漏报，按 id 升序
    first_confluence_batch_ids: list[int]


class ImpactPreviewOut(BaseModel):
    # 请求无序对归一后按 id 升序返回
    low_sample_id: int
    high_sample_id: int
    affected_batches: list[ImpactPreviewBatchOut]  # 已同时含两样本的混样批次，按 id 升序、各一次


class RecipeItem(BaseModel):
    batch_id: StrictPositiveInt
    portions: StrictPositiveInt


class PoolCreate(BaseModel):
    name: BatchName
    recipe: Annotated[list[RecipeItem], Field(min_length=1)]  # 拒绝空配方


class RecipeItemOut(BaseModel):
    batch_id: int
    batch_name: str
    portions: int


class BatchOut(BaseModel):
    id: int
    name: BatchName
    kind: Literal["sample", "pool"]
    created_at: str
    recipe: list[RecipeItemOut] | None = None  # 仅混样批次返回配方


class ComponentOut(BaseModel):
    sample_id: int
    sample_name: str
    numerator: int    # 约分后的分子
    denominator: int  # 约分后的分母


class CompositionOut(BaseModel):
    batch_id: int
    batch_name: BatchName
    kind: Literal["sample", "pool"]
    components: list[ComponentOut]


class RecipePreviewRequest(BaseModel):
    """完整替代配方（与创建混样相同：非空、严格正整数份数、重复原料合并）。"""

    recipe: Annotated[list[RecipeItem], Field(min_length=1)]  # 拒绝空配方


class FractionOut(BaseModel):
    numerator: int    # 约分后的分子（有符号）
    denominator: int  # 约分后的分母（恒为正）


class PreviewComponentOut(BaseModel):
    sample_id: int
    sample_name: str
    original: FractionOut
    preview: FractionOut
    delta: FractionOut  # 有符号差值 = preview - original


class AffectedBatchOut(BaseModel):
    batch_id: int
    batch_name: BatchName
    kind: Literal["sample", "pool"]
    components: list[PreviewComponentOut]


class RecipePreviewOut(BaseModel):
    batch_id: int
    batch_name: BatchName
    recipe: list[RecipeItemOut]  # 合并重复原料后的替代配方
    affected_batches: list[AffectedBatchOut]  # 目标批次 + 全部下游批次，各只出现一次


class PoolPlanRequest(BaseModel):
    """混样规划：给定两个已有原料与目标样本占比，反算正整数投入份数。"""

    ingredient_a_batch_id: StrictPositiveInt
    ingredient_b_batch_id: StrictPositiveInt
    sample_id: StrictPositiveInt
    numerator: StrictNonNegativeInt
    denominator: StrictPositiveInt
    max_total_portions: StrictPositiveInt

    @model_validator(mode="after")
    def _validate(self) -> PoolPlanRequest:
        if self.ingredient_a_batch_id == self.ingredient_b_batch_id:
            raise ValueError("两个原料批次必须不同")
        if self.numerator > self.denominator:
            raise ValueError("目标占比必须满足 0 ≤ 分子 ≤ 分母")
        return self


class PoolPlanRecipeItemOut(BaseModel):
    batch_id: int
    portions: int  # 正整数，可直接作为 /pools 的 recipe 项提交


class PoolPlanOut(BaseModel):
    sample_id: int
    target: FractionOut  # 约分后的目标占比
    total_portions: int  # 最小总份数 = recipe 中份数之和，且 ≤ max_total_portions
    recipe: list[PoolPlanRecipeItemOut]  # 顺序与请求中两个原料一致，可直接提交 /pools
    components: list[ComponentOut]  # 新混样的全部原始样本占比（约分后的分子/分母）


class PortionMeasurement(BaseModel):
    """一个原始样本的实测占比（未列出的样本视为 0）。"""

    sample_id: StrictPositiveInt
    numerator: StrictNonNegativeInt
    denominator: StrictPositiveInt


class PortionDiagnosisRequest(BaseModel):
    """份数诊断：实测各原始样本占比 + 纠正份数上限。

    假设目标混样配方中仅一项投入份数记错，其余不变，反推该项的正整数份数。
    """

    measurements: list[PortionMeasurement]
    max_portions: StrictPositiveInt

    @model_validator(mode="after")
    def _validate(self) -> PortionDiagnosisRequest:
        ids = [m.sample_id for m in self.measurements]
        if len(ids) != len(set(ids)):
            raise ValueError("实测样本 id 不得重复")
        # 未列出的样本占比视为 0，故列出的占比之和必须恰好为 1
        if sum(Fraction(m.numerator, m.denominator) for m in self.measurements) != 1:
            raise ValueError("实测样本占比之和必须为 1")
        return self


class PortionDiagnosisSolutionOut(BaseModel):
    ingredient_batch_id: int
    ingredient_name: str
    original_portions: int   # 配方中当前记录的份数
    corrected_portions: int  # 反推出的正整数份数（与原值不同且不超过上限）


class PortionDiagnosisUndeterminedOut(BaseModel):
    """份数变化不改变组成且与实测吻合的项：无法判定，不枚举份数。"""

    ingredient_batch_id: int
    ingredient_name: str
    original_portions: int


class PortionDiagnosisOut(BaseModel):
    batch_id: int
    batch_name: BatchName
    solutions: list[PortionDiagnosisSolutionOut]         # 按原料 id 升序，无解为空列表
    undetermined: list[PortionDiagnosisUndeterminedOut]  # 按原料 id 升序


class InventoryAddRequest(BaseModel):
    """为已有原始样本补入库存（正整数份数，累加到当前余额）。"""

    portions: StrictPositiveInt


class InventoryOut(BaseModel):
    sample_id: int
    sample_name: str
    balance: int     # 当前库存余额（份），未补入过的原始样本为 0
    updated_at: str | None = None  # 最近一次补入时间；从未补入时为 null


class PreparationCreateRequest(BaseModel):
    """批次制备：正整数制备次数（每制备一次按配方消耗一批）。"""

    times: StrictPositiveInt


class PreparationConsumptionOut(BaseModel):
    sample_id: int
    sample_name: str
    portions: int  # 本次制备沿配方图逐层乘算、多路径合并后消耗的整数份数


class PreparationOut(BaseModel):
    id: int
    batch_id: int
    batch_name: BatchName
    times: int  # 制备次数（正整数）
    created_at: str
    consumptions: list[PreparationConsumptionOut]  # 各原始样本整数消耗，按样本 id 升序
    remaining_inventory: list[InventoryOut]        # 扣减后的剩余库存，按样本 id 升序


class PreparationJobItem(BaseModel):
    """整单制备中的一项：已有批次 id + 正整数制备次数。"""

    batch_id: StrictPositiveInt
    times: StrictPositiveInt


class PreparationJobCreateRequest(BaseModel):
    """整单制备：非空清单；同一批次重复出现时由服务端先合并次数。"""

    items: Annotated[list[PreparationJobItem], Field(min_length=1)]  # 拒绝空清单


class PreparationJobBatchOut(BaseModel):
    """整单中一个批次（合并重复项后）的制备记录。"""

    batch_id: int
    batch_name: BatchName
    preparation_id: int  # 可由 GET /preparations/{id} 读取的制备记录 id
    times: int           # 合并后的制备次数（正整数）
    consumptions: list[PreparationConsumptionOut]  # 该批次沿配方图的整数消耗，按样本 id 升序


class PreparationJobOut(BaseModel):
    preparations: list[PreparationJobBatchOut]  # 各批次制备记录，按批次 id 升序、各一次
    consumptions: list[PreparationConsumptionOut]  # 整单按样本汇总的总消耗，按样本 id 升序


class SelectionPreviewRequest(BaseModel):
    """只读排产预演的候选清单：1～12 条候选制备单。

    每条与整单制备项同构（已有批次 id + 正整数次数），但订单按请求位置
    （从 1 开始）标识：允许同一批次重复出现，重复批次不合并次数，
    各自作为互相独立的候选订单参与选型。
    """

    items: Annotated[list[PreparationJobItem], Field(min_length=1, max_length=12)]


class SelectionPreviewOut(BaseModel):
    # 入选订单的请求位置（从 1 开始），按位置升序；库存全不足时为空列表
    selected_positions: list[int]
    # 未入选订单的请求位置（从 1 开始），按位置升序
    unselected_positions: list[int]
    # 入选订单沿配方图展开后按样本汇总的整数消耗，按样本 id 升序
    consumptions: list[PreparationConsumptionOut]
    # 同一库存快照下满足入选消耗后的剩余库存（只列被消耗样本），按样本 id 升序
    remaining_inventory: list[InventoryOut]
