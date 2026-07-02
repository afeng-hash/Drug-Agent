"""
Inventory node — 库存和价格查询。

在 Recommend 节点之后执行，查询推荐药品的库存情况，
将库存信息追加到 response 末尾，供用户参考购买。

输出格式示例：
  ## 📦 库存情况
  - **布洛芬缓释胶囊** | 0.3g×24粒 | 某某制药
    💰 ¥18.50 | ✅ 有货 | 📍 A-3-2
  - **对乙酰氨基酚片** | 500mg×12片 | 另一制药
    💰 ¥12.00 | ✅ 有货 (库存紧张，仅剩3件) | 📍 B-1-5
"""

from app.db.repositories.drug import DrugRepository
from app.db.repositories.inventory import InventoryRepository


async def inventory_node(
    state: dict,
    inventory_repo: InventoryRepository,
    drug_repo: DrugRepository,
) -> dict:
    """查询推荐药品的库存，并将库存信息追加到 response。

    Args:
        state:         当前对话状态
        inventory_repo: 库存仓库（已绑定 DB session）
        drug_repo:      药品仓库（用于查替代药品）

    Returns:
        state 更新 dict：
          - response     → 原 response + 库存信息（追加模式）
          - node_events  → 节点事件日志
    """
    recommendations = state.get("recommendations", [])
    if not recommendations:
        return {
            "node_events": [{"node": "inventory", "drugs_checked": 0}],
        }

    # ── 批量查询库存 ──
    drug_ids = [r["drug_id"] for r in recommendations]
    inventory_items = await inventory_repo.find_by_drugs(drug_ids)

    # 按 drug_id 分组（一个药品可能有多个库存SKU）
    grouped: dict[int, list] = {}
    for item in inventory_items:
        grouped.setdefault(item.drug_id, []).append(item)

    # ── 格式化库存信息 ──
    lines = ["\n## 📦 库存情况\n"]

    for rec in recommendations:
        drug_id = rec["drug_id"]
        generic_name = rec["generic_name"]
        items = grouped.get(drug_id, [])

        if items:
            for item in items:
                # 有货 vs 缺货
                status = "✅ 有货" if item.stock_quantity > 0 else "❌ 缺货"
                # 库存紧张提醒（少于 10 件）
                stock_note = (
                    f"(库存紧张，仅剩{item.stock_quantity}件)"
                    if 0 < item.stock_quantity < 10
                    else ""
                )
                lines.append(
                    f"- **{item.product_name}** | {item.specification} | "
                    f"{item.manufacturer}\n"
                    f"  💰 ¥{item.price:.2f} | {status} {stock_note} | "
                    f"📍 {item.shelf_location}"
                )
        else:
            lines.append(f"- **{generic_name}** — ⚠️ 暂无库存信息")

        # ── 推荐替代药品 ──
        # 如果某药品完全没库存，尝试找同症状的替代品（相同有效成分不同商品名）
        if not items:
            alternatives = await drug_repo.find_by_symptoms(
                [generic_name], category="感冒退烧"
            )
            alt_names = [
                d.generic_name for d in alternatives
                if d.generic_name != generic_name
            ]
            if alt_names:
                lines.append(f"  💡 同成分替代：{'、'.join(alt_names[:2])}")

    # ── 追加到已有 response ──
    response = state.get("response", "") + "\n".join(lines)

    return {
        "response": response,
        "node_events": [{"node": "inventory", "drugs_checked": len(recommendations)}],
    }
