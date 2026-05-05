import os
from models import Product, ProductSKU, ShippingTemplate


def upsert_product(graph_client, product_id, merchant_id):
    if not graph_client.enabled:
        return False

    source_tag = os.getenv('KG_SOURCE_TAG', 'mall_assistant_v2')
    product = Product.query.get(product_id)
    if not product:
        return False

    graph_client.run(
        """
        MERGE (m:Merchant {merchant_id:$merchant_id})
        SET m.source_tag=$source_tag
        MERGE (p:Product {product_id:$product_id, merchant_id:$merchant_id})
        SET p.name=$name, p.category=$category, p.origin=$origin, p.is_on_sale=$is_on_sale, p.source_tag=$source_tag
        MERGE (m)-[:SELLS]->(p)
        """,
        merchant_id=merchant_id,
        product_id=product.product_id,
        name=product.name,
        category=product.category,
        origin=product.origin,
        is_on_sale=bool(product.is_on_sale),
        source_tag=source_tag,
    )

    if product.shipping_template_id:
        template = ShippingTemplate.query.get(product.shipping_template_id)
        if template:
            graph_client.run(
                """
                MERGE (st:ShippingTemplate {template_id:$template_id})
                SET st.name=$name, st.base_cost=$base_cost, st.source_tag=$source_tag
                WITH st
                MATCH (p:Product {product_id:$product_id, merchant_id:$merchant_id})
                MERGE (p)-[:USES_SHIPPING]->(st)
                """,
                template_id=template.template_id,
                source_tag=source_tag,
                name=template.name,
                base_cost=float(template.base_cost),
                product_id=product.product_id,
                merchant_id=merchant_id,
            )

    skus = ProductSKU.query.filter_by(product_id=product.product_id).all()
    for sku in skus:
        graph_client.run(
            """
            MATCH (p:Product {product_id:$product_id, merchant_id:$merchant_id})
            MERGE (s:SKU {sku_id:$sku_id, merchant_id:$merchant_id})
            SET s.spec_name=$spec_name, s.price=$price, s.stock=$stock, s.source_tag=$source_tag
            MERGE (p)-[:HAS_SKU]->(s)
            """,
            product_id=product.product_id,
            merchant_id=merchant_id,
            sku_id=sku.sku_id,
            spec_name=sku.spec_name,
            price=float(sku.price),
            stock=int(sku.stock or 0),
            source_tag=source_tag,
        )
    return True
