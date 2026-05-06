from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from decimal import Decimal  # 确保 Decimal 导入

# 初始化数据库对象
db = SQLAlchemy()


# ==========================================
# 1. 用户体系 (User & Farmer)
# ==========================================

class User(db.Model):
    """用户主表：存储消费者、农户和管理员的基本信息"""
    __tablename__ = 'T_User'
    user_id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    # 角色: 0-消费者, 1-农户, 2-管理员
    role = db.Column(db.Integer, default=0)

    # 状态: 1-正常/已审核, 2-待审核/冻结
    status = db.Column(db.Integer, default=1)

    # 联系方式 (个人中心编辑)
    email = db.Column(db.String(100))
    phone = db.Column(db.String(20))

    # 关联关系
    farmer_info = db.relationship('FarmerInfo', backref='user', uselist=False)
    products = db.relationship('Product', backref='farmer', lazy=True)
    posts = db.relationship('CommunityPost', backref='author', lazy=True)

    # 订单与购物车关联 (现在关联到 SKU)
    cart_items = db.relationship('CartItem', backref='user', lazy=True)
    orders = db.relationship('Order', backref='customer', lazy=True)


class FarmerInfo(db.Model):
    """农户详情表：存储店铺专属信息"""
    __tablename__ = 'T_Farmer_Info'
    farmer_id = db.Column(db.Integer, db.ForeignKey('T_User.user_id'), primary_key=True)
    shop_name = db.Column(db.String(100))  # 店铺名称
    contact_person = db.Column(db.String(50))  # 联系人姓名
    farm_address = db.Column(db.String(255))  # 农场/发货地址
    bio = db.Column(db.Text)  # 店铺简介


# ==========================================
# 2. 商品与社区体系 (Product & Community)
# ==========================================

# 🔥 [新增] 运费模板表
class ShippingTemplate(db.Model):
    """运费模板表：用于计算商品的运费"""
    __tablename__ = 'T_Shipping_Template'
    template_id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    base_cost = db.Column(db.Numeric(10, 2), default=Decimal('10.00'))  # 基础运费，后续可扩展为按地区/重量计算


class Product(db.Model):
    """农产品主表"""
    __tablename__ = 'T_Product'
    product_id = db.Column(db.Integer, primary_key=True)
    farmer_id = db.Column(db.Integer, db.ForeignKey('T_User.user_id'), nullable=False)

    name = db.Column(db.String(100), nullable=False)  # 商品名称
    category = db.Column(db.String(50))  # 分类
    origin = db.Column(db.String(100))  # 产地

    # price = db.Column(db.Numeric(10, 2), nullable=False)
    # stock = db.Column(db.Integer, default=0)

    description = db.Column(db.Text)  # 详细描述
    image_url = db.Column(db.String(255))  # 图片链接

    # 🔥 [新增] 上下架状态
    is_on_sale = db.Column(db.Boolean, default=True)  # True=上架, False=下架

    # 🔥 [新增] 关联运费模板
    shipping_template_id = db.Column(db.Integer, db.ForeignKey('T_Shipping_Template.template_id'), nullable=True)
    shipping_template = db.relationship('ShippingTemplate')

    # 🔥 [新增] SKU 关联
    skus = db.relationship('ProductSKU', backref='product', lazy=True)


# 🔥 [新增] 商品规格 SKU 表
class ProductSKU(db.Model):
    """商品规格表：存储具体的价格和库存信息"""
    __tablename__ = 'T_Product_SKU'
    sku_id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('T_Product.product_id'), nullable=False)

    spec_name = db.Column(db.String(100), nullable=False)  # 例如: '大果 5斤装'
    price = db.Column(db.Numeric(10, 2), nullable=False)  # SKU 价格
    stock = db.Column(db.Integer, default=0)  # SKU 库存

# 🔥 [修复核心点] 创建别名，完美兼容我们在智能客服模块中写的 from models import SKU
SKU = ProductSKU


class CommunityPost(db.Model):
    """社区帖子表"""
    __tablename__ = 'T_Community_Post'
    post_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('T_User.user_id'), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    content = db.Column(db.Text, nullable=False)
    post_date = db.Column(db.DateTime, default=datetime.now)
    views = db.Column(db.Integer, default=0)

    # 🔥 [新增] 社区帖子图片 URL 字段
    image_url = db.Column(db.String(255), nullable=True)

    # 🔥 [新增] 关联商品ID (允许为空)
    related_product_id = db.Column(db.Integer, db.ForeignKey('T_Product.product_id'), nullable=True)

    # 🔥 [新增] 关联关系
    related_product = db.relationship('Product', backref='related_posts', lazy=True)


# ==========================================
# 3. 推荐系统核心数据 (CF Engine Data)
# 注意：BehaviorLog 和 ItemSimilarity 暂时保留关联 Product ID
# ==========================================

class BehaviorLog(db.Model):
    """用户行为日志表 (CF算法原材料)"""
    __tablename__ = 'T_Behavior_Log'
    log_id = db.Column(db.BigInteger, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('T_User.user_id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('T_Product.product_id'), nullable=False)

    # 行为类型: 1: 点击, 2: 收藏, 3: 加购, 4: 购买
    behavior_type = db.Column(db.Integer, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.now)


class ItemSimilarity(db.Model):
    """物品相似度表 (CF算法离线计算结果)"""
    __tablename__ = 'T_Item_Similarity'
    item_a_id = db.Column(db.Integer, db.ForeignKey('T_Product.product_id'), primary_key=True)
    item_b_id = db.Column(db.Integer, db.ForeignKey('T_Product.product_id'), primary_key=True)

    similarity_score = db.Column(db.Float, nullable=False)  # 相似度得分
    update_date = db.Column(db.Date, default=datetime.now)  # 计算时间


# ==========================================
# 4. 交易与订单体系 (Shopping Cart & Order)
# ==========================================

class CartItem(db.Model):
    """购物车项"""
    __tablename__ = 'T_Cart_Item'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('T_User.user_id'), nullable=False)
    # 🔥 [修改] 从关联 Product 改为关联 ProductSKU
    sku_id = db.Column(db.Integer, db.ForeignKey('T_Product_SKU.sku_id'), nullable=False)
    quantity = db.Column(db.Integer, default=1)

    # product = db.relationship('Product') # 移除
    sku = db.relationship('ProductSKU')  # 新增 SKU 关联

    # 🔥 [修改] 唯一约束使用 sku_id
    __table_args__ = (db.UniqueConstraint('user_id', 'sku_id', name='_user_sku_uc'),)


class Order(db.Model):
    """订单主表"""
    __tablename__ = 'T_Order'
    order_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('T_User.user_id'), nullable=False)
    total_amount = db.Column(db.Numeric(10, 2), nullable=False)

    # 状态: 1-待支付, 2-待发货, 3-待收货, 4-已完成, 5-已取消, 🔥 6-售后中, 7-已退款/售后完成
    status = db.Column(db.Integer, default=1)
    order_date = db.Column(db.DateTime, default=datetime.now)

    address = db.Column(db.String(255))
    receiver_name = db.Column(db.String(50))
    receiver_phone = db.Column(db.String(20))

    # 🔥 [新增] 订单运费
    shipping_cost = db.Column(db.Numeric(10, 2), default=Decimal('0.00'))

    # 🔥 [新增] 追踪/发货信息
    tracking_number = db.Column(db.String(100))  # 发货单号
    after_sales_reason = db.Column(db.Text)  # 售后原因

    items = db.relationship('OrderItem', backref='order', lazy=True)


class OrderItem(db.Model):
    """订单详情表"""
    __tablename__ = 'T_Order_Item'
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('T_Order.order_id'), nullable=False)
    # 🔥 [修改] 从关联 Product 改为关联 ProductSKU
    sku_id = db.Column(db.Integer, db.ForeignKey('T_Product_SKU.sku_id'), nullable=False)
    farmer_id = db.Column(db.Integer, db.ForeignKey('T_User.user_id'), nullable=False)
    # 保留 product_id 和 product_name 作为非外键的历史记录，方便查阅
    product_id = db.Column(db.Integer, nullable=False)
    product_name = db.Column(db.String(100))

    quantity = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Numeric(10, 2), nullable=False)  # 记录成交价

    # product = db.relationship('Product') # 移除
    sku = db.relationship('ProductSKU')  # 新增 SKU 关联


# ==========================================
# 5. 聊天与消息体系 (Chat & Message)
# ==========================================

class Conversation(db.Model):
    """会话表：存储用户之间的聊天会话"""
    __tablename__ = 'T_Conversation'
    id = db.Column(db.Integer, primary_key=True)
    # 存储参与者的ID，例如 '1,10' (保证ID小的在前，用于唯一性索引)
    participants = db.Column(db.String(255), nullable=False, index=True)
    last_message_date = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    messages = db.relationship('Message', backref='conversation', lazy='dynamic')

    # 确保用户对之间只有一个会话，并允许模型重复加载
    __table_args__ = (
        db.UniqueConstraint('participants', name='_unique_participants_uc'),
        {'extend_existing': True}
    )


class Message(db.Model):
    """消息表：存储每条消息记录"""
    __tablename__ = 'T_Message'
    id = db.Column(db.BigInteger, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey('T_Conversation.id'), nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey('T_User.user_id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.now, index=True)
    # 消息状态：0-未读, 1-已读
    status = db.Column(db.Integer, default=0)

    sender = db.relationship('User', foreign_keys=[sender_id], backref='sent_messages')

    # 允许模型重复加载
    __table_args__ = {'extend_existing': True}


class ProductReview(db.Model):
    """商品评价表：用户针对已购买订单的商品进行评价"""
    __tablename__ = 'T_Product_Review'
    review_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('T_User.user_id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('T_Product.product_id'), nullable=False)  # 评分目标
    order_id = db.Column(db.Integer, db.ForeignKey('T_Order.order_id'), nullable=False)  # 关联订单

    rating = db.Column(db.Integer, nullable=False)  # 1-5星
    content = db.Column(db.Text)  # 评论内容
    review_date = db.Column(db.DateTime, default=datetime.now)

    # 确保一个订单只能评价一次 (核心要求)
    __table_args__ = (db.UniqueConstraint('order_id', name='_unique_order_review'),)

    # 关联
    product = db.relationship('Product', backref='reviews')
    user = db.relationship('User', backref='reviews')

class MerchantPolicy(db.Model):
    __tablename__ = 'T_Merchant_Policy'
    id = db.Column(db.Integer, primary_key=True)
    merchant_id = db.Column(db.Integer, db.ForeignKey('T_User.user_id'), nullable=False, index=True)
    return_window_days = db.Column(db.Integer, default=7)
    supports_no_reason_return = db.Column(db.Boolean, default=False)
    fresh_goods_rule = db.Column(db.String(255), default='生鲜类非质量问题不支持无理由退货')
    shipping_bearer_rule = db.Column(db.String(255), default='质量问题商家承担运费，非质量问题买家承担')
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)


class FAQItem(db.Model):
    __tablename__ = 'T_FAQ_Item'
    id = db.Column(db.Integer, primary_key=True)
    merchant_id = db.Column(db.Integer, db.ForeignKey('T_User.user_id'), nullable=False, index=True)
    question = db.Column(db.String(255), nullable=False)
    answer = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(50), default='售前')
    is_active = db.Column(db.Boolean, default=True)


class KGSyncLog(db.Model):
    __tablename__ = 'T_KG_Sync_Log'
    id = db.Column(db.BigInteger, primary_key=True)
    merchant_id = db.Column(db.Integer, db.ForeignKey('T_User.user_id'), nullable=False, index=True)
    entity_type = db.Column(db.String(50), nullable=False)
    entity_id = db.Column(db.Integer, nullable=False)
    action = db.Column(db.String(20), nullable=False)
    sync_status = db.Column(db.String(20), default='success')
    error_message = db.Column(db.Text)
    version = db.Column(db.String(50), default='v1')
    created_at = db.Column(db.DateTime, default=datetime.now)