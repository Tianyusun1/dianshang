import os
from app import app, db
from models import User, FarmerInfo, Product, ProductSKU
from werkzeug.security import generate_password_hash
from decimal import Decimal
from sqlalchemy.exc import IntegrityError
import random
from datetime import datetime

from services.kg.graph_client import GraphClient
from services.kg.kg_builder import upsert_product

# --- 配置 ---
# 确保与您的 config.py 里的 UPLOAD_FOLDER 配置一致
UPLOAD_PREFIX = '/static/uploads/'

# 运费模板 ID (来自 app.py/models.py 中的 ShippingTemplate 初始化)
DEFAULT_SHIPPING = 1  # 默认运费 (¥10.00)
COLD_CHAIN_SHIPPING = 2  # 冷链运费 (¥25.00)
FREE_SHIPPING = 3  # 免运费 (¥0.00)

# 图片中识别的商品信息及其对应的分类和运费模板ID
# 类别已严格对齐：'水果', '蔬菜', '肉禽蛋', '粮油', '特产'
PRODUCT_DATA = [
    # 农户 1: 水果鲜果 (farmer_fruit)
    {"farmer": 1, "name": "高山有机红富士苹果", "category": "水果", "origin": "山东烟台", "image": "apple.png",
     "shipping": DEFAULT_SHIPPING, "specs": [
        {"spec_name": "5斤装", "price": 45.00, "stock": 150},
        {"spec_name": "10斤礼盒", "price": 85.00, "stock": 80}
    ]},
    {"farmer": 1, "name": "海南特产皇帝蕉", "category": "水果", "origin": "海南乐东", "image": "banana.png",
     "shipping": FREE_SHIPPING, "specs": [
        {"spec_name": "串装 3kg", "price": 30.50, "stock": 200},
        {"spec_name": "精品 1kg", "price": 12.00, "stock": 300}
    ]},
    {"farmer": 1, "name": "新鲜丰水梨", "category": "水果", "origin": "河北赵州", "image": "pear.png",
     "shipping": DEFAULT_SHIPPING, "specs": [
        {"spec_name": "净重 5斤", "price": 48.00, "stock": 180}
    ]},
    {"farmer": 1, "name": "精品丹东草莓", "category": "水果", "origin": "辽宁丹东", "image": "strawberry.png",
     "shipping": COLD_CHAIN_SHIPPING, "specs": [
        {"spec_name": "礼盒 500g", "price": 68.80, "stock": 100}
    ]},
    {"farmer": 1, "name": "宁夏硒砂瓜", "category": "水果", "origin": "宁夏中卫", "image": "watermelon.png",
     "shipping": DEFAULT_SHIPPING, "specs": [
        {"spec_name": "整瓜 10斤", "price": 99.90, "stock": 50}
    ]},
    {"farmer": 1, "name": "新鲜蓝莓", "category": "水果", "origin": "吉林长白山", "image": "精品蓝莓.png",
     "shipping": COLD_CHAIN_SHIPPING, "specs": [
        {"spec_name": "250g 盒装", "price": 35.00, "stock": 120}
    ]},
    {"farmer": 1, "name": "华南小玉米", "category": "蔬菜", "origin": "云南曲靖", "image": "华南小玉米.png",
     "shipping": DEFAULT_SHIPPING, "specs": [
        {"spec_name": "5斤散装", "price": 28.00, "stock": 100}
    ]},

    # 农户 2: 蔬菜 & 粮油米面 (farmer_veg)
    {"farmer": 2, "name": "农家新鲜白菜", "category": "蔬菜", "origin": "河南周口", "image": "白菜.png", "shipping": FREE_SHIPPING,
     "specs": [
         {"spec_name": "净重 5kg", "price": 18.00, "stock": 500}
     ]},
    {"farmer": 2, "name": "有机菠菜", "category": "蔬菜", "origin": "河北张家口", "image": "蔬菜.png", "shipping": FREE_SHIPPING,
     "specs": [
         {"spec_name": "500g 袋装", "price": 8.00, "stock": 400}
     ]},
    {"farmer": 2, "name": "非转基因菜籽油", "category": "粮油", "origin": "四川成都", "image": "菜籽油.png",
     "shipping": DEFAULT_SHIPPING, "specs": [
        {"spec_name": "5L 大桶装", "price": 108.00, "stock": 90}
    ]},
    {"farmer": 2, "name": "特级橄榄油", "category": "粮油", "origin": "新疆克拉玛依", "image": "橄榄油.png",
     "shipping": DEFAULT_SHIPPING, "specs": [
        {"spec_name": "500ml 瓶装", "price": 65.00, "stock": 110}
    ]},
    {"farmer": 2, "name": "高筋面粉", "category": "粮油", "origin": "内蒙古巴彦淖尔", "image": "高筋面粉.png",
     "shipping": DEFAULT_SHIPPING, "specs": [
        {"spec_name": "2.5kg 袋装", "price": 38.00, "stock": 250}
    ]},
    {"farmer": 2, "name": "精品海鱼", "category": "肉禽蛋", "origin": "山东威海", "image": "海鱼.png",
     "shipping": COLD_CHAIN_SHIPPING, "specs": [
        {"spec_name": "500g 冰鲜", "price": 55.00, "stock": 150}
    ]},
    {"farmer": 2, "name": "农家土豆", "category": "蔬菜", "origin": "宁夏固原", "image": "土豆.png", "shipping": DEFAULT_SHIPPING,
     "specs": [
         {"spec_name": "5kg 散装", "price": 25.00, "stock": 350}
     ]},
    {"farmer": 2, "name": "东北五常大米", "category": "粮油", "origin": "黑龙江五常", "image": "五常米.png",
     "shipping": DEFAULT_SHIPPING, "specs": [
        {"spec_name": "10斤袋装", "price": 128.00, "stock": 100}
    ]},
    {"farmer": 2, "name": "西蓝花", "category": "蔬菜", "origin": "福建漳州", "image": "西兰花.png", "shipping": DEFAULT_SHIPPING,
     "specs": [
         {"spec_name": "单棵 500g", "price": 15.00, "stock": 200}
     ]},
    {"farmer": 2, "name": "新鲜玉米", "category": "蔬菜", "origin": "吉林松原", "image": "玉米.png", "shipping": DEFAULT_SHIPPING,
     "specs": [
         {"spec_name": "10根真空装", "price": 40.00, "stock": 150}
     ]},
    {"farmer": 2, "name": "圆白菜", "category": "蔬菜", "origin": "甘肃兰州", "image": "圆白菜.png", "shipping": DEFAULT_SHIPPING,
     "specs": [
         {"spec_name": "单颗 1kg", "price": 12.00, "stock": 250}
     ]},

    # 农户 3: 肉禽蛋 & 特产 (farmer_meat)
    {"farmer": 3, "name": "农家散养土鸡蛋", "category": "肉禽蛋", "origin": "湖北黄冈", "image": "鸡蛋.png",
     "shipping": COLD_CHAIN_SHIPPING, "specs": [
        {"spec_name": "30枚礼盒装", "price": 75.00, "stock": 80}
    ]},
    {"farmer": 3, "name": "鲜活青虾", "category": "肉禽蛋", "origin": "江苏南通", "image": "青虾.png",
     "shipping": COLD_CHAIN_SHIPPING, "specs": [
        {"spec_name": "500g 冰鲜", "price": 120.00, "stock": 60}
    ]},
    {"farmer": 3, "name": "新鲜生菜", "category": "蔬菜", "origin": "山东寿光", "image": "生菜.png", "shipping": FREE_SHIPPING,
     "specs": [
         {"spec_name": "500g 袋装", "price": 9.90, "stock": 250}
     ]},
    {"farmer": 3, "name": "精品食用盐", "category": "特产", "origin": "四川自贡", "image": "食盐.png", "shipping": FREE_SHIPPING,
     "specs": [
         {"spec_name": "500g 罐装", "price": 5.50, "stock": 1000}
     ]},
    {"farmer": 3, "name": "内蒙羔羊肉片", "category": "肉禽蛋", "origin": "内蒙古呼伦贝尔", "image": "羊肉.png",
     "shipping": COLD_CHAIN_SHIPPING, "specs": [
        {"spec_name": "火锅切片 500g", "price": 88.00, "stock": 120}
    ]},
    {"farmer": 3, "name": "农家黑猪肉", "category": "肉禽蛋", "origin": "安徽皖南", "image": "猪肉.png",
     "shipping": COLD_CHAIN_SHIPPING, "specs": [
        {"spec_name": "五花肉 500g", "price": 45.00, "stock": 100}
    ]},
    {"farmer": 3, "name": "散养土鸡", "category": "肉禽蛋", "origin": "江西赣州", "image": "土鸡.png",
     "shipping": COLD_CHAIN_SHIPPING, "specs": [
        {"spec_name": "整只 2kg", "price": 158.00, "stock": 50}
    ]},
    {"farmer": 3, "name": "散养黑鹅", "category": "肉禽蛋", "origin": "浙江湖州", "image": "鹅.png",
     "shipping": COLD_CHAIN_SHIPPING, "specs": [
        {"spec_name": "整只 3kg", "price": 268.00, "stock": 40}
    ]},
    {"farmer": 3, "name": "老抽酱油", "category": "特产", "origin": "广东佛山", "image": "老抽.png", "shipping": DEFAULT_SHIPPING,
     "specs": [
         {"spec_name": "特级 500ml", "price": 18.00, "stock": 150}
     ]},
    {"farmer": 3, "name": "特级牛肉", "category": "肉禽蛋", "origin": "吉林长春", "image": "牛肉.png",
     "shipping": COLD_CHAIN_SHIPPING, "specs": [
        {"spec_name": "西冷牛排 250g", "price": 95.00, "stock": 70}
    ]},
    {"farmer": 3, "name": "山西陈醋", "category": "特产", "origin": "山西太原", "image": "山西陈醋.png", "shipping": DEFAULT_SHIPPING,
     "specs": [
         {"spec_name": "酿造 1L", "price": 32.00, "stock": 150}
     ]},
    {"farmer": 3, "name": "新鲜胡萝卜", "category": "蔬菜", "origin": "山东聊城", "image": "胡萝卜.png", "shipping": DEFAULT_SHIPPING,
     "specs": [
         {"spec_name": "5斤散装", "price": 20.00, "stock": 220}
     ]},
]


def create_farmer(username, password, shop_name, farm_address, contact_person):
    """创建并激活一个农户账号"""
    try:
        user = User.query.filter_by(username=username).first()
        if user:
            print(f"⚠️ 农户 {username} 已存在 (ID: {user.user_id})，跳过创建。")
            return user.user_id

        user = User(
            username=username,
            password_hash=generate_password_hash(password),
            role=1,  # 农户
            status=1  # 已审核通过
        )
        db.session.add(user)
        db.session.flush()  # 获取 user_id

        farmer_info = FarmerInfo(
            farmer_id=user.user_id,
            shop_name=shop_name,
            contact_person=contact_person,
            farm_address=farm_address
        )
        db.session.add(farmer_info)
        db.session.commit()
        print(f"✅ 农户创建成功: {username} (ID: {user.user_id})")
        return user.user_id
    except IntegrityError:
        db.session.rollback()
        print(f"❌ 农户 {username} 创建失败: 完整性错误。")
        return None
    except Exception as e:
        db.session.rollback()
        print(f"❌ 农户 {username} 创建失败: {e}")
        return None


def add_products(farmer_id, product_list):
    """为指定农户批量添加商品"""
    for data in product_list:
        # 检查商品是否已存在
        if Product.query.filter_by(farmer_id=farmer_id, name=data['name']).first():
            # print(f"  - ⏭️ 商品 {data['name']} 已存在，跳过。") # 减少输出
            continue

        try:
            # 使用静态图片文件名，假设它们位于 static/uploads/
            image_filename = data['image']
            image_url = UPLOAD_PREFIX + image_filename

            # 1. 创建商品主表
            new_product = Product(
                farmer_id=farmer_id,
                name=data['name'],
                category=data['category'],
                origin=data['origin'],
                description=f"来自 {data['origin']} 的 {data['category']} 精品，绿色健康，产地直发。",
                image_url=image_url,
                is_on_sale=True,
                shipping_template_id=data['shipping']
            )
            db.session.add(new_product)
            db.session.flush()

            # 2. 创建 SKUs
            for spec in data['specs']:
                sku = ProductSKU(
                    product_id=new_product.product_id,
                    spec_name=spec['spec_name'],
                    # 确保价格是 Decimal 类型
                    price=Decimal(str(spec['price'])),
                    # 随机化库存，增加数据真实性
                    stock=random.randint(max(1, spec['stock'] - 30), spec['stock'] + 30)
                )
                db.session.add(sku)

            db.session.commit()
            print(f"  - ✅ 已添加商品: {data['name']} (类别: {data['category']})")

        except Exception as e:
            db.session.rollback()
            print(f"❌ 添加商品 {data['name']} 失败: {e}")
            continue


def sync_farmer_products_to_kg(graph_client, farmer_id):
    """将某农户当前所有商品同步到知识图谱（含新增与历史数据）。"""
    if not graph_client.enabled:
        print(f"   ⚠️ KG 未启用，跳过同步: farmer_id={farmer_id}")
        return

    products = Product.query.filter_by(farmer_id=farmer_id).all()
    success_count = 0
    for product in products:
        if upsert_product(graph_client, product.product_id, farmer_id):
            success_count += 1
    print(f"   🧠 KG 同步完成: farmer_id={farmer_id}, {success_count}/{len(products)}")


def init_test_data():
    with app.app_context():
        db.create_all()

        print("--- 启动测试数据初始化 ---")

        # 1. 创建并激活农户 (密码统一为 123456)
        farmer1_id = create_farmer("farmer_fruit", "123456", "果香园直营店", "山东烟台市某高山果园", "李果农")
        farmer2_id = create_farmer("farmer_veg", "123456", "绿色田园菜篮子", "河北张家口市万亩蔬菜基地", "王菜农")
        farmer3_id = create_farmer("farmer_meat", "123456", "内蒙草原牧场", "内蒙古呼伦贝尔大草原深处", "赵牧民")

        if not os.getenv("KG_SOURCE_TAG"):
            os.environ["KG_SOURCE_TAG"] = f"mall_assistant_{datetime.now().strftime('%Y%m%d')}"

        graph_client = GraphClient()
        print(f"[KG] enabled={graph_client.enabled}, database={graph_client.database}, source_tag={os.getenv('KG_SOURCE_TAG')}")

        # 2. 批量添加商品
        if farmer1_id:
            print(f"\n--- 为农户 'farmer_fruit' (ID: {farmer1_id}) 添加商品 ---")
            add_products(farmer1_id, [p for p in PRODUCT_DATA if p['farmer'] == 1])
            sync_farmer_products_to_kg(graph_client, farmer1_id)

        if farmer2_id:
            print(f"\n--- 为农户 'farmer_veg' (ID: {farmer2_id}) 添加商品 ---")
            add_products(farmer2_id, [p for p in PRODUCT_DATA if p['farmer'] == 2])
            sync_farmer_products_to_kg(graph_client, farmer2_id)

        if farmer3_id:
            print(f"\n--- 为农户 'farmer_meat' (ID: {farmer3_id}) 添加商品 ---")
            add_products(farmer3_id, [p for p in PRODUCT_DATA if p['farmer'] == 3])
            sync_farmer_products_to_kg(graph_client, farmer3_id)

        print("\n--- 测试数据初始化完成 ---")


if __name__ == '__main__':
    init_test_data()