from pymilvus import MilvusClient

# --- 1. 连接配置 ---
# 请根据你的 Milvus 服务信息修改以下参数
URI = "http://localhost:19530"  # Milvus 服务地址
# 如果你的 Milvus 启用了认证，请取消注释并修改下面一行
# TOKEN = "username:password"

# --- 2. 连接到 Milvus ---
try:
    print(f"正在连接到 Milvus 服务: {URI}")
    client = MilvusClient(uri=URI)
    print("✅ 连接成功！")
except Exception as e:
    print(f"❌ 连接失败: {str(e)}")
    exit(1)

# --- 3. 列出所有集合 ---
try:
    collections = client.list_collections()
    if not collections:
        print("\n⚠️ 当前数据库中没有集合。")
        exit(0)

    print("\n--- 数据库中的集合列表 ---")
    for i, col in enumerate(collections):
        print(f"{i + 1}. {col}")
    print("-----------------------------")

    # --- 4. 查询第一个集合的数据 ---
    # 你可以将 target_collection 改为你想查询的特定集合名
    target_collection = collections[0]
    print(f"\n正在查询集合 '{target_collection}' 的前 5 条数据...")

    # 使用 query 方法查询所有数据，并限制返回数量
    result = client.query(
        collection_name=target_collection,
        filter="",  # 空过滤条件，表示查询所有
        output_fields=["*"],  # 返回所有字段
        limit=5
    )

    if not result:
        print(f"⚠️ 集合 '{target_collection}' 中没有数据。")
    else:
        print(f"\n--- 集合 '{target_collection}' 的前 5 条数据 ---")
        for i, row in enumerate(result):
            print(f"记录 {i + 1}: {row}")
        print("-------------------------------------------------")

except Exception as e:
    print(f"❌ 操作过程中发生错误: {str(e)}")
finally:
    client.close()
    print("\n连接已关闭。")