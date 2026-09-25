"""混样批次的原始样本占比计算。

全程使用 fractions.Fraction 做精确有理数运算，避免浮点误差；
Fraction 会自动约分，分子/分母即为最简形式。
"""
from __future__ import annotations

import math
import sqlite3
from fractions import Fraction

Composition = dict[int, Fraction]  # {原始样本 id: 占比}
Recipes = dict[int, list[tuple[int, int]]]  # {混样批次 id: [(原料批次 id, 份数)]}
# 制备单位整数消耗：{批次 id: {原始样本 id: 制备一次所需整数份数}}
UnitNeeds = dict[int, dict[int, int]]


class PoolPlanError(ValueError):
    """混样规划在业务上无解（目标不可达或最小解超过总份数上限），对应 HTTP 422。"""


def _load_graph(conn: sqlite3.Connection) -> tuple[dict[int, str], Recipes]:
    """读取全部批次类型与配方，构成内存中的配方引用图。"""
    kinds = {row["id"]: row["kind"] for row in conn.execute("SELECT id, kind FROM batches")}

    recipes: Recipes = {}
    for row in conn.execute("SELECT pool_id, ingredient_id, portions FROM pool_recipes"):
        recipes.setdefault(row["pool_id"], []).append((row["ingredient_id"], row["portions"]))
    return kinds, recipes


def _expand(
    kinds: dict[int, str],
    recipes: Recipes,
    batch_id: int,
    memo: dict[int, Composition],
) -> Composition:
    """递归展开批次；memo 让多路径汇合的批次只精确计算一次。"""
    if batch_id in memo:
        return memo[batch_id]
    if kinds[batch_id] == "sample":
        memo[batch_id] = {batch_id: Fraction(1)}
        return memo[batch_id]
    items = recipes.get(batch_id, [])
    total = sum(portions for _, portions in items)
    result: Composition = {}
    for ingredient_id, portions in items:
        share = Fraction(portions, total)
        for sample_id, frac in _expand(kinds, recipes, ingredient_id, memo).items():
            result[sample_id] = result.get(sample_id, Fraction(0)) + share * frac
    memo[batch_id] = result
    return result


def reachable_samples(conn: sqlite3.Connection, ingredient_ids: list[int]) -> set[int]:
    """沿配方图递归收集若干原料批次可达的全部原始样本 id。

    包含嵌套混样与多路径汇合：只要某原始样本经任意路径可达即被收集。
    配方图是有向无环图（新批次只能引用更早创建的批次），遍历必然终止。
    份数不影响可达性，故此处只需图结构，不做分数运算。
    """
    kinds = {row["id"]: row["kind"] for row in conn.execute("SELECT id, kind FROM batches")}
    recipes: dict[int, list[int]] = {}
    for row in conn.execute("SELECT pool_id, ingredient_id FROM pool_recipes"):
        recipes.setdefault(row["pool_id"], []).append(row["ingredient_id"])

    samples: set[int] = set()
    seen: set[int] = set()
    stack = list(ingredient_ids)
    while stack:
        node = stack.pop()
        if node in seen:  # 多路径汇合：同一批次只展开一次
            continue
        seen.add(node)
        if kinds.get(node) == "sample":
            samples.add(node)
        else:
            stack.extend(recipes.get(node, []))
    return samples


def batch_composition(conn: sqlite3.Connection, batch_id: int) -> Composition:
    """递归展开批次，返回 {原始样本 id: 占比}。

    - 每一层按“投入份数 / 该层总份数”分摊占比；
    - 同一原始样本经多条路径到达时，占比相加合并；
    - 新批次只能引用已创建的批次，引用关系天然是有向无环图，递归必然终止。
    """
    kinds, recipes = _load_graph(conn)
    return _expand(kinds, recipes, batch_id, {})


def exclusion_impact_preview(
    conn: sqlite3.Connection, sample_a: int, sample_b: int
) -> tuple[int, int, list[tuple[int, bool, list[int]]]]:
    """禁配补录影响预演：沿现有批次配方 DAG 计算两个原始样本的可达性。

    返回 (low_id, high_id, [(受影响批次 id, 是否首次合流, 可追溯首次合流批次 id 列表)])：
    - 受影响批次 = 沿配方图可达原始样本中同时含两样本的混样批次，按 id 升序、各一次；
    - 首次合流 = 自身同时含两样本、但每个直接原料都不同时含两样本的批次；
    - 可追溯首次合流：沿配方边向上追溯，自身为首次合流时含自身，否则为各原料
      追溯结果的并集；嵌套与多路径汇合经 memo + 集合并去重，不漏报不重复。
    全程只读：不登记规则、不改任何配方。
    """
    kinds, recipes = _load_graph(conn)
    low_id, high_id = sorted((sample_a, sample_b))
    pair = frozenset({low_id, high_id})

    reach_memo: dict[int, frozenset[int]] = {}

    def reachable(batch_id: int) -> frozenset[int]:
        """沿配方 DAG 收集批次可达的全部原始样本；多路径汇合只展开一次。"""
        if batch_id in reach_memo:
            return reach_memo[batch_id]
        if kinds[batch_id] == "sample":
            result: frozenset[int] = frozenset({batch_id})
        else:
            result = frozenset().union(
                *(reachable(ingredient_id) for ingredient_id, _ in recipes.get(batch_id, []))
            )
        reach_memo[batch_id] = result
        return result

    affected = [
        batch_id
        for batch_id in sorted(b for b, kind in kinds.items() if kind == "pool")
        if pair <= reachable(batch_id)
    ]

    def ingredient_ids(batch_id: int) -> list[int]:
        return [ingredient_id for ingredient_id, _ in recipes.get(batch_id, [])]

    # 首次合流：直接原料中没有任何一个同时含两样本
    first_confluence = {
        batch_id: all(not pair <= reachable(ingredient_id)
                      for ingredient_id in ingredient_ids(batch_id))
        for batch_id in affected
    }

    trace_memo: dict[int, frozenset[int]] = {}

    def trace_first(batch_id: int) -> frozenset[int]:
        """沿配方边向上追溯全部首次合流批次 id。

        非受影响原料（不同时含两样本，含原始样本自身）追溯结果为空：首次合流
        只可能出现在“同时含两样本”的子 DAG 上。
        """
        if batch_id in trace_memo:
            return trace_memo[batch_id]
        # 不含两样本的批次不可能通向首次合流（可达集是原料可达集的并集）
        if not pair <= reachable(batch_id):
            result = frozenset()
        elif first_confluence.get(batch_id):
            result = frozenset({batch_id})
        else:
            result = frozenset().union(
                *(trace_first(ingredient_id) for ingredient_id in ingredient_ids(batch_id))
            )
        trace_memo[batch_id] = result
        return result

    return low_id, high_id, [
        (batch_id, first_confluence[batch_id], sorted(trace_first(batch_id)))
        for batch_id in affected
    ]


def downstream_batches(recipes: Recipes, batch_id: int) -> list[int]:
    """沿配方引用关系找出全部下游批次（含目标自身）。

    下游 = 直接或间接把目标批次当作原料的混样批次；
    多路径汇合时同一批次只收集一次，结果按 id 升序返回。
    """
    consumers: dict[int, list[int]] = {}
    for pool_id, items in recipes.items():
        for ingredient_id, _ in items:
            consumers.setdefault(ingredient_id, []).append(pool_id)

    seen = {batch_id}
    stack = [batch_id]
    while stack:
        for consumer in consumers.get(stack.pop(), []):
            if consumer not in seen:
                seen.add(consumer)
                stack.append(consumer)
    return sorted(seen)


def recipe_preview(
    conn: sqlite3.Connection, batch_id: int, new_recipe: dict[int, int]
) -> dict[int, tuple[Composition, Composition]]:
    """在内存中把 batch_id 的配方替换为 new_recipe，评估影响，不修改数据库。

    返回 {受影响批次 id: (原占比, 预演占比)}；新配方只允许引用比目标批次
    更早创建的批次（由调用方校验），替换后引用图仍是无环图，递归必然终止。
    """
    kinds, recipes = _load_graph(conn)
    affected = downstream_batches(recipes, batch_id)

    preview_recipes = dict(recipes)
    preview_recipes[batch_id] = sorted(new_recipe.items())

    original_memo: dict[int, Composition] = {}
    preview_memo: dict[int, Composition] = {}
    return {
        bid: (
            _expand(kinds, recipes, bid, original_memo),
            _expand(kinds, preview_recipes, bid, preview_memo),
        )
        for bid in affected
    }


def pool_plan(
    conn: sqlite3.Connection,
    ingredient_a: int,
    ingredient_b: int,
    sample_id: int,
    target: Fraction,
    max_total_portions: int,
) -> tuple[int, int, Composition]:
    """反算两个原料的正整数投入份数 (portions_a, portions_b)，使 sample_id 在
    新混样（仅由这两个原料按份数混合）中的占比恰好等于 target。

    设两原料中该样本的精确占比分别为 a、b（沿用 _expand 的分数展开），新混样
    占比 = (a·x + b·y) / (x + y) = target（x、y 为正整数份数）。一般情况下该
    方程的正整数解构成一族 x:y 固定的等比解，把比值约到最简即得到总份数最小
    的唯一解；当 a == b == target 时任意份数都满足，按约定取 1:1。无正整数解
    或最小总份数超过上限时抛 PoolPlanError。

    返回 (portions_a, portions_b, 新混样全部原始样本占比)。
    """
    kinds, recipes = _load_graph(conn)
    memo: dict[int, Composition] = {}
    comp_a = _expand(kinds, recipes, ingredient_a, memo)
    comp_b = _expand(kinds, recipes, ingredient_b, memo)

    a = comp_a.get(sample_id, Fraction(0))
    b = comp_b.get(sample_id, Fraction(0))

    if a == b:
        # 两原料中该样本占比相同：只有目标也相等时才有解，且任意正整数份数都满足，
        # 按要求取总份数最小的 1:1；否则目标在区间之外。
        if a != target:
            raise PoolPlanError(
                f"目标占比 {target.numerator}/{target.denominator} 无法用正整数份数达到: "
                f"两原料中该样本占比均为 {a.numerator}/{a.denominator}"
            )
        portions_a, portions_b = 1, 1
    else:
        # 目标必须严格落在两原料占比之间：区间之外（含只与一端相等）时，
        # 正份数加权平均不可能达到——相等端点会迫使另一原料投入 0 份。
        if not (min(a, b) < target < max(a, b)):
            raise PoolPlanError(
                f"目标占比 {target.numerator}/{target.denominator} 无法用正整数份数达到: "
                f"两原料中该样本占比为 {a.numerator}/{a.denominator}、{b.numerator}/{b.denominator}"
            )
        # (a - target)·x = (target - b)·y，两侧同号非零；交叉相乘后约到最简
        left = a - target
        right = target - b
        x_num = abs(right.numerator * left.denominator)
        y_num = abs(left.numerator * right.denominator)
        g = math.gcd(x_num, y_num)
        portions_a = x_num // g
        portions_b = y_num // g

    total = portions_a + portions_b
    if total > max_total_portions:
        raise PoolPlanError(
            f"最小整数解总份数为 {total}，超过上限 {max_total_portions}"
        )

    share_a = Fraction(portions_a, total)
    share_b = Fraction(portions_b, total)
    composition: Composition = {}
    for sid, frac in comp_a.items():
        composition[sid] = composition.get(sid, Fraction(0)) + share_a * frac
    for sid, frac in comp_b.items():
        composition[sid] = composition.get(sid, Fraction(0)) + share_b * frac
    return portions_a, portions_b, composition


def portion_diagnosis(
    conn: sqlite3.Connection,
    batch_id: int,
    measured: dict[int, Fraction],
    max_portions: int,
) -> tuple[list[tuple[int, int, int]], list[tuple[int, int]]]:
    """假设混样 batch_id 的配方中仅一项投入份数记错，反推该项的正整数份数。

    measured 为 {原始样本 id: 实测占比}（未列出的样本视为 0，总和已为 1，
    由调用方校验）。设被记错的项为 j（记录份数 p_j，实际份数 x），其余项
    份数之和 R 与其贡献 B_s 固定，则实际组成为 (B_s + x·A_s)/(R + x)，
    其中 A_s 为 j 沿嵌套配方精确展开的样本 s 占比。对每个样本得到有理数
    方程 x·(A_s − m_s) = m_s·R − B_s，任取 A_s ≠ m_s 的样本即可解出唯一
    候选 x，再用全部样本占比逐一复核。

    返回 (solutions, undetermined)，均按原料 id 升序：
    - solutions: [(原料批次 id, 原份数, 纠正份数)]，只含与原值不同且不超过
      max_portions 的解；
    - undetermined: [(原料批次 id, 原份数)]，份数变化不改变组成（B_s = A_s·R）
      且组成恰与实测吻合（A_s = m_s）的项——任意正整数份数都成立，无法判定，
      不枚举份数。
    不写库。
    """
    kinds, recipes = _load_graph(conn)
    recipe = recipes.get(batch_id, [])
    memo: dict[int, Composition] = {}
    comps = {ingredient_id: _expand(kinds, recipes, ingredient_id, memo)
             for ingredient_id, _ in recipe}

    total = sum(portions for _, portions in recipe)
    # 全部相关样本：实测列出的 ∪ 任一原料展开含有的（未列出的实测占比视为 0）
    all_samples = set(measured)
    for comp in comps.values():
        all_samples.update(comp)

    # 当前配方下各样本的未归一化分子 N_s = Σ_i p_i·comp_i(s)，
    # 去掉项 j 后即其余项的贡献 B_s = N_s − p_j·A_s
    numerator_sum = {
        s: sum((portions * comps[i].get(s, Fraction(0)) for i, portions in recipe),
               Fraction(0))
        for s in all_samples
    }

    solutions: list[tuple[int, int, int]] = []
    undetermined: list[tuple[int, int]] = []
    for ingredient_id, original in sorted(recipe):
        rest = total - original  # R：其余项份数之和
        comp_j = comps[ingredient_id]

        def others_contrib(s: int) -> Fraction:
            return numerator_sum[s] - original * comp_j.get(s, Fraction(0))

        # 份数变化不改变组成且与实测吻合：任意正整数份数都成立，无法判定
        if all(
            comp_j.get(s, Fraction(0)) == measured.get(s, Fraction(0))
            and others_contrib(s) == measured.get(s, Fraction(0)) * rest
            for s in all_samples
        ):
            undetermined.append((ingredient_id, original))
            continue

        # 任取一个 A_s ≠ m_s 的样本解出唯一候选 x；若不存在这样的样本
        # （A_s 恒等于 m_s 但某项 B_s ≠ m_s·R），则任何 x 都不满足，无解
        candidate: Fraction | None = None
        for s in all_samples:
            a_s = comp_j.get(s, Fraction(0))
            m_s = measured.get(s, Fraction(0))
            if a_s != m_s:
                candidate = (m_s * rest - others_contrib(s)) / (a_s - m_s)
                break
        if candidate is None or candidate.denominator != 1 or candidate < 1:
            continue
        corrected = candidate.numerator
        # 只保留不同于原值且不超过上限的解
        if corrected == original or corrected > max_portions:
            continue
        # 用全部样本占比复核（未列出的样本实测占比视为 0）
        if all(
            (others_contrib(s) + corrected * comp_j.get(s, Fraction(0)))
            / (rest + corrected)
            == measured.get(s, Fraction(0))
            for s in all_samples
        ):
            solutions.append((ingredient_id, original, corrected))
    return solutions, undetermined


def _unit_needs(
    kinds: dict[int, str],
    recipes: Recipes,
    batch_id: int,
    memo: UnitNeeds,
) -> dict[int, int]:
    """计算制备一批次一次所需各原始样本的整数份数（单位消耗向量）。

    - 原始样本制备一次直接消耗自身 1 份；
    - 混样制备一次按配方消耗各直接原料“份数”次，各原料的单位消耗按份数
      逐层相乘；同一原始样本经多条配方路径到达时需求相加合并；
    - 配方份数与制备次数都是正整数，逐层乘算、合并的结果恒为整数，
      无需分数运算；memo 让多路径汇合的批次只计算一次。
    """
    if batch_id in memo:
        return memo[batch_id]
    if kinds[batch_id] == "sample":
        memo[batch_id] = {batch_id: 1}
        return memo[batch_id]
    needs: dict[int, int] = {}
    for ingredient_id, portions in recipes.get(batch_id, []):
        for sample_id, unit in _unit_needs(kinds, recipes, ingredient_id, memo).items():
            needs[sample_id] = needs.get(sample_id, 0) + portions * unit
    memo[batch_id] = needs
    return needs


def preparation_requirements(
    conn: sqlite3.Connection, batch_id: int, times: int
) -> dict[int, int]:
    """制备 batch_id 共 times 次所需各原始样本的整数份数。

    返回 {原始样本 id: 份数}，只含需求为正的样本，按 id 升序由调用方处理。
    """
    kinds, recipes = _load_graph(conn)
    units = _unit_needs(kinds, recipes, batch_id, {})
    return {sample_id: unit * times for sample_id, unit in units.items()}


def preparation_job_requirements(
    conn: sqlite3.Connection, items: list[tuple[int, int]]
) -> dict[int, dict[int, int]]:
    """整单制备：计算每个批次（批次 id, 合并后的制备次数）对原始样本的整数需求。

    返回 {批次 id: {原始样本 id: 份数}}，只含需求为正的样本。所有批次共用同一份
    配方图与单位消耗 memo：不同批次经多路径汇合到同一中间批次时只计算一次；
    整单按样本汇总的总需求由调用方求和（共享样本必须按汇总后的总需求核算，
    不能让各项分别判为足量）。
    """
    kinds, recipes = _load_graph(conn)
    memo: UnitNeeds = {}
    return {
        batch_id: {
            sample_id: unit * times
            for sample_id, unit in _unit_needs(kinds, recipes, batch_id, memo).items()
        }
        for batch_id, times in items
    }


def select_preparation_subset(
    demands: list[dict[int, int]],
    times: list[int],
    balances: dict[int, int],
) -> tuple[list[int], list[int]]:
    """只读排产选型：从候选制备单中选出库存可承载的子集。

    - demands[i]、times[i] 为第 i 条候选订单（0 起下标）按配方图展开的
      {原始样本 id: 整数需求} 与制备次数，三者等长；
    - balances 为同一库存快照下各样本余额（未列出的样本视为 0）；
    - 可行子集：入选订单对每一样本的总需求都不超过该样本余额（空集恒可行）；
    - 选型优先级：先最大化入选订单数，再最大化入选制备次数之和，
      仍并列时取入选位置序列（1 起位置，升序）字典序最小者。

    返回 (selected, unselected)：均为 1 起请求位置、按位置升序的列表。
    纯内存计算，不读不写数据库。候选最多 12 条，直接枚举全部子集。
    """
    n = len(demands)
    best_key: tuple[int, int, tuple[int, ...]] | None = None
    best_positions: tuple[int, ...] = ()
    for mask in range(1 << n):
        total: dict[int, int] = {}
        count = 0
        times_sum = 0
        for i in range(n):
            if not mask >> i & 1:
                continue
            count += 1
            times_sum += times[i]
            for sample_id, portions in demands[i].items():
                total[sample_id] = total.get(sample_id, 0) + portions
        if any(total[sid] > balances.get(sid, 0) for sid in total):
            continue
        positions = tuple(i + 1 for i in range(n) if mask >> i & 1)
        # 订单数、次数之和越大越优；位置序列字典序越小越优（元组比较即序列字典序）
        key = (count, times_sum, tuple(-p for p in positions))
        if best_key is None or key > best_key:
            best_key = key
            best_positions = positions
    selected = set(best_positions)
    return (
        list(best_positions),
        [i + 1 for i in range(n) if i + 1 not in selected],
    )
