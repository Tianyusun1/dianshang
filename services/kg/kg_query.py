def find_product_facts(graph_client, merchant_id, keyword):
    return graph_client.run(
        """
        MATCH (m:Merchant {merchant_id:$merchant_id})-[:SELLS]->(p:Product)-[:HAS_SKU]->(s:SKU)
        WHERE p.name CONTAINS $keyword OR p.category CONTAINS $keyword
        RETURN p.product_id as product_id, p.name as product_name, p.category as category,
               s.sku_id as sku_id, s.spec_name as spec_name, s.price as price, s.stock as stock
        ORDER BY s.price ASC
        LIMIT 10
        """,
        merchant_id=merchant_id,
        keyword=keyword,
    )
