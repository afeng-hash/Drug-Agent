"""Inventory node — query stock/pricing for recommended drugs."""

from app.db.repositories.drug import DrugRepository
from app.db.repositories.inventory import InventoryRepository


async def inventory_node(
    state: dict,
    inventory_repo: InventoryRepository,
    drug_repo: DrugRepository,
) -> dict:
    """Query inventory for recommended drugs and format result.

    Args:
        state: Current ConversationState.
        inventory_repo: Injected InventoryRepository.
        drug_repo: Injected DrugRepository.

    Returns:
        State updates including response (appended inventory info).
    """
    recommendations = state.get("recommendations", [])
    if not recommendations:
        return {
            "node_events": [{"node": "inventory", "drugs_checked": 0}],
        }

    drug_ids = [r["drug_id"] for r in recommendations]
    inventory_items = await inventory_repo.find_by_drugs(drug_ids)

    # Group by drug_id
    grouped: dict[int, list] = {}
    for item in inventory_items:
        grouped.setdefault(item.drug_id, []).append(item)

    lines = ["\n## 📦 库存情况\n"]
    for rec in recommendations:
        drug_id = rec["drug_id"]
        generic_name = rec["generic_name"]
        items = grouped.get(drug_id, [])

        if items:
            for item in items:
                status = "✅ 有货" if item.stock_quantity > 0 else "❌ 缺货"
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

        # Check for alternatives if out of stock
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

    response = state.get("response", "") + "\n".join(lines)

    return {
        "response": response,
        "node_events": [{"node": "inventory", "drugs_checked": len(recommendations)}],
    }
