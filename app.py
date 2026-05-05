import os
import time  # <-- 引入 time 模块用于生成唯一文件名
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
from config import Config
from decimal import Decimal
from sqlalchemy import or_, func, create_engine, text, select
from sqlalchemy.orm import joinedload  # <-- 引入 joinedload 用于优化查询
from models import db, User, CommunityPost, FarmerInfo, Product, \
    BehaviorLog, ItemSimilarity, CartItem, Order, OrderItem, ProductSKU, ShippingTemplate, \
    Conversation, Message, ProductReview  # <-- 🔥 新增 ProductReview
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy.exc import IntegrityError
from datetime import datetime

from recommend import RecommenderEngine
from routes.assistant import assistant_bp

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)
app.register_blueprint(assistant_bp)

# 🔥 修复：统一 SocketIO 初始化方式，并移除 cors_allowed_origins，因为在 Canvas 环境下通常不需要显式设置
socketio = SocketIO(app)

# 🔥 修复：调整 RecommenderEngine 初始化时机和方式（假设 RecommenderEngine 类接受 Flask app 实例）
# 这里暂时使用原版传入 app 的方式，并假设它在内部处理了依赖。
recommender = RecommenderEngine(app)

# ==========================================
# 辅助功能初始化
# ==========================================
if not hasattr(app.config, 'UPLOAD_FOLDER') or not app.config['UPLOAD_FOLDER']:
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    app.config['UPLOAD_FOLDER'] = os.path.join(BASE_DIR, 'static', 'uploads')

if not os.path.exists(app.config['UPLOAD_FOLDER']):
    try:
        os.makedirs(app.config['UPLOAD_FOLDER'])
        print(f"✅ 上传目录已创建: {app.config['UPLOAD_FOLDER']}")
    except Exception as e:
        print(f"❌ 创建上传目录失败: {e}")


def allowed_file(filename):
    allowed_exts = app.config.get('ALLOWED_EXTENSIONS', {'png', 'jpg', 'jpeg', 'gif'})
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in allowed_exts


def init_shipping_templates():
    # 确保使用 Decimal 类型，避免数据库类型不匹配
    if ShippingTemplate.query.count() == 0:
        default_template = ShippingTemplate(template_id=1, name="默认运费", base_cost=Decimal('10.00'))
        cold_chain_template = ShippingTemplate(template_id=2, name="冷链运费", base_cost=Decimal('25.00'))
        # 新增免运费模板 (ID=3)
        free_shipping_template = ShippingTemplate(template_id=3, name="免运费", base_cost=Decimal('0.00'))

        db.session.add_all([default_template, cold_chain_template, free_shipping_template])
        db.session.commit()
        print("✅ 默认运费模板已初始化 (ID 1, 2, 3)！")



def ensure_database_exists():
    """若数据库不存在则自动创建（MySQL）。"""
    uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    if not uri.startswith('mysql'):
        return

    try:
        db_name = uri.rsplit('/', 1)[-1].split('?', 1)[0]
        server_uri = uri.rsplit('/', 1)[0]
        engine = create_engine(server_uri)
        with engine.connect() as conn:
            conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
            conn.commit()
        print(f"✅ 数据库已确认存在: {db_name}")
    except Exception as e:
        print(f"❌ 自动创建数据库失败，请检查数据库账号权限: {e}")

# ==========================================
# 数据库初始化
# ==========================================
with app.app_context():
    try:
        ensure_database_exists()
        db.create_all()
        print("✅ 数据库表已检测/创建成功！")

        init_shipping_templates()

    except Exception as e:
        print(f"❌ 数据库连接失败，请检查 config.py 里的密码: {e}")


@app.context_processor
def inject_user():
    user = None
    if 'user_id' in session:
        user = db.session.get(User, session['user_id'])
    return dict(current_user=user)


def calculate_shipping_cost(cart_items, address):
    """
    根据传入的购物车项列表或运费模板ID，计算运费。
    当传入列表时，取所有商品的运费模板中最高的 base_cost 作为最终运费 (简化逻辑)。
    """
    if not cart_items:
        return Decimal('0.00')

    # Case 1: Template ID is passed as an integer (e.g. from place_order for a single item.
    if isinstance(cart_items, int):
        template = db.session.get(ShippingTemplate, cart_items)
        return template.base_cost if template else Decimal('10.00')

    # Case 2: List of CartItem objects is passed (from checkout). This is the key fix.
    if isinstance(cart_items, list):
        max_cost = Decimal('0.00')

        # 查找购物车中所有商品的运费模板 ID
        template_ids = {
            item.sku.product.shipping_template_id
            for item in cart_items
            if item.sku and item.sku.product and item.sku.product.shipping_template_id
        }

        # 遍历所有唯一的模板 ID，获取最高运费
        for template_id in template_ids:
            template = db.session.get(ShippingTemplate, template_id)
            if template:
                # 运费取所有模板中最高的那个
                max_cost = max(max_cost, template.base_cost)

        return max_cost

    # Fallback for unexpected input
    return Decimal('10.00')


# 🔥 关键修复：可复用的推荐商品获取与格式化函数 (已修改逻辑以确保返回列表长度)
def get_formatted_recommendations(user_id, num_recommendations=4):
    """获取并格式化推荐商品列表（包括 min_price 和 SKU信息）"""
    if not user_id:
        return []

    # 1. 初始化查询基础
    base_query = Product.query.filter(
        Product.is_on_sale == True
    ).options(joinedload(Product.skus))

    products_to_return = []
    recommended_ids = []

    # 2. 尝试从推荐引擎获取 CF 推荐 ID
    try:
        with app.app_context():
            # 获取推荐 ID 列表
            recommended_ids = recommender.get_recommendations(user_id, num_recommendations=num_recommendations)

        if recommended_ids:
            # 按照推荐顺序加载 CF 商品
            rec_products_unsorted = base_query.filter(
                Product.product_id.in_(recommended_ids)
            ).all()

            product_map = {p.product_id: p for p in rec_products_unsorted}
            # 保持推荐顺序并过滤掉已下架/不存在的商品
            products_to_return = [product_map[pid] for pid in recommended_ids if pid in product_map]

    except Exception as e:
        # 兜底：如果推荐引擎出错，记录错误
        print(f"⚠️ 推荐算法调用失败: {e}")

    # === 🔥 关键修复：混合热门商品进行填充（Padding） ===

    # 3. 检查数量是否足够，如果不足，从热门商品中补充
    if len(products_to_return) < num_recommendations:
        # 获取当前已推荐的 ID 集合，避免重复
        existing_ids = {p.product_id for p in products_to_return}
        num_needed = num_recommendations - len(products_to_return)

        # 补充热门商品 (按ID降序，并且必须是未被CF推荐的)
        # 这里的 Product.product_id.desc() 就是实现“排名靠前”的简易逻辑
        hot_products = base_query.filter(
            Product.is_on_sale == True,
            Product.product_id.notin_(existing_ids)
        ).order_by(Product.product_id.desc()).limit(num_needed).all()

        # 将热门商品添加到返回列表
        products_to_return.extend(hot_products)

    # 4. 格式化输出 (适配模板需要的 product, min_price 结构)
    final_products_data = []
    for product in products_to_return:
        min_price = min(sku.price for sku in product.skus) if product.skus else Decimal('0.00')
        final_products_data.append({
            'product': product,
            'min_price': min_price
        })

    # 确保最终只返回 num_recommendations 个
    return final_products_data[:num_recommendations]

# ==========================================
# 🌐 核心页面路由
# ==========================================

@app.route('/')
def index():
    """商城首页：已接入核心推荐算法"""
    q = request.args.get('q', '')

    base_query = Product.query.filter(Product.is_on_sale == True).options(joinedload(Product.skus))  # 预加载 skus

    if q:
        products = base_query.filter(
            or_(
                Product.name.contains(q),
                Product.category.contains(q),
                Product.origin.contains(q)
            )
        ).all()
        recommendation_msg = f"🔍 搜索结果: '{q}'"

    else:
        fallback_products = base_query.order_by(Product.product_id.desc()).all()
        products = fallback_products
        recommendation_msg = "🔥 热门农产品推荐"

        if 'user_id' in session:
            try:
                # 注意: recommender.get_recommendations 可能需要 app_context
                with app.app_context():
                    recommended_ids = recommender.get_recommendations(session['user_id'])

                if recommended_ids:
                    # 按照推荐顺序加载商品，并确保它们仍然在售
                    rec_products_unsorted = Product.query.filter(
                        Product.product_id.in_(recommended_ids),
                        Product.is_on_sale == True
                    ).options(joinedload(Product.skus)).all()

                    product_map = {p.product_id: p for p in rec_products_unsorted}

                    sorted_products = [product_map[pid] for pid in recommended_ids if pid in product_map]

                    if sorted_products:
                        products = sorted_products
                        recommendation_msg = "✨ 猜你喜欢 (为您定制)"
            except Exception as e:
                # 兼容旧版本 recommender 初始化
                print(f"⚠️ 推荐算法调用失败，已回退到热门列表: {e}")

    # 处理商品列表，确保它们有 min_price 用于模板渲染 (index.html 需要这个结构)
    final_products_data = []
    for product in products:
        min_price = min(sku.price for sku in product.skus) if product.skus else Decimal('0.00')
        final_products_data.append({
            'product': product,
            'min_price': min_price
        })

    return render_template('index.html', products=final_products_data, search_query=q,
                           recommendation_msg=recommendation_msg)


@app.route('/product/<int:product_id>')
def product_detail(product_id):
    """商品详情页：接入 Item-Based 协同过滤 -> 热门商品兜底 + 收藏状态检查"""
    product = Product.query.get_or_404(product_id)

    current_user = db.session.get(User, session.get('user_id'))
    if not product.is_on_sale and (not current_user or current_user.role == 0):
        flash('🚫 该商品已下架或正在维护中。')
        return redirect(url_for('index'))

    has_favorited = False
    if 'user_id' in session:
        fav_log = BehaviorLog.query.filter_by(
            user_id=session['user_id'],
            product_id=product_id,
            behavior_type=2
        ).first()
        if fav_log:
            has_favorited = True

        try:
            new_log = BehaviorLog(
                user_id=session['user_id'],
                product_id=product_id,
                behavior_type=1  # 点击行为
            )
            db.session.add(new_log)
            db.session.commit()
        except:
            pass

    recommendations = []

    # --- 推荐逻辑 ---
    # 这里只是从数据库加载 ItemSimilarity 结果，如果推荐引擎没有运行，这里可能为空。
    similar_items = ItemSimilarity.query.filter(
        or_(ItemSimilarity.item_a_id == product_id, ItemSimilarity.item_b_id == product_id)
    ).order_by(ItemSimilarity.similarity_score.desc()).limit(4).all()

    if similar_items:
        related_ids = []
        for item in similar_items:
            target_id = item.item_b_id if item.item_a_id == product_id else item.item_a_id
            related_ids.append(target_id)

        if related_ids:
            # 预加载 SKU
            products_unsorted = Product.query.filter(
                Product.product_id.in_(related_ids),
                Product.is_on_sale == True
            ).options(joinedload(Product.skus)).all()

            product_map = {p.product_id: p for p in products_unsorted}

            # 🔥 确保 recommendations 传递给模板时包含 SKU 信息
            recommendations = [product_map[pid] for pid in related_ids if pid in product_map]

    if not recommendations:
        # Fallback 1: 热门商品/同类商品推荐... (略)
        pass

    product_skus = ProductSKU.query.filter_by(product_id=product_id).order_by(ProductSKU.price.asc()).all()

    # --- 🔥 新增：商品评价统计与列表 ---
    # 获取所有评价，并预加载用户
    all_reviews = ProductReview.query.filter_by(product_id=product_id).options(joinedload(ProductReview.user)).order_by(
        ProductReview.review_date.desc()).all()

    total_reviews_count = len(all_reviews)
    average_rating = Decimal('0.0')

    if total_reviews_count > 0:
        # 使用 func.sum 计算总分
        total_rating = db.session.query(func.sum(ProductReview.rating)).filter_by(product_id=product_id).scalar()
        # 计算平均分，保留一位小数
        average_rating = round(Decimal(str(total_rating)) / Decimal(str(total_reviews_count)), 1)
    # --- 结束：商品评价统计与列表 ---

    # 转换为模板所需的结构
    recommendations_for_template = []
    for rec_product in recommendations:
        # 确保每个推荐商品包含 skus 属性
        rec_product_skus = rec_product.skus
        min_rec_price = min(s.price for s in rec_product_skus) if rec_product_skus else Decimal('0.00')

        recommendations_for_template.append({
            'product_id': rec_product.product_id,
            'name': rec_product.name,
            'image_url': rec_product.image_url,
            'category': rec_product.category,
            'skus': rec_product_skus,  # 传递完整的 skus 列表
            'min_price': min_rec_price  # 方便模板直接使用
        })

    return render_template('product_detail.html',
                           product=product,
                           recommendations=recommendations_for_template,  # <-- 使用转换后的列表
                           has_favorited=has_favorited,
                           product_skus=product_skus,
                           reviews=all_reviews,  # <-- 新增
                           average_rating=average_rating,  # <-- 新增
                           total_reviews_count=total_reviews_count)  # <-- 新增


# ==========================================
# 🏘️ 社区功能
# ==========================================

@app.route('/community')
def community():
    # 🔥 优化查询：一次性加载作者和关联商品及其SKU
    posts_query = CommunityPost.query \
        .options(joinedload(CommunityPost.author), joinedload(CommunityPost.related_product).joinedload(Product.skus)) \
        .order_by(CommunityPost.post_date.desc()).all()

    posts_data = []
    for post in posts_query:
        min_price = None
        if post.related_product:
            min_price = min(sku.price for sku in post.related_product.skus) if post.related_product.skus else Decimal(
                '0.00')

        posts_data.append({
            'post': post,
            'min_price': min_price
        })

    return render_template('community.html', posts=posts_data)


@app.route('/community/delete/<int:post_id>', methods=['POST'])
def delete_post(post_id):
    """删除社区帖子，仅限作者或管理员操作"""
    if 'user_id' not in session:
        flash('请先登录。')
        return redirect(url_for('login'))

    user = db.session.get(User, session['user_id'])
    post = CommunityPost.query.get_or_404(post_id)

    if post.user_id != user.user_id and user.role != 2:
        flash('🚫 权限不足，无法删除此帖子。', 'error')
        return redirect(url_for('community'))

    try:
        db.session.delete(post)
        db.session.commit()
        flash('✅ 帖子已成功删除。')
    except Exception as e:
        db.session.rollback()
        flash(f'❌ 删除失败: {e}', 'error')

    return redirect(url_for('community'))


@app.route('/community/new', methods=['GET', 'POST'])
def new_post():
    """发布新帖子 (支持关联商品和图片)"""
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user = db.session.get(User, session['user_id'])

    if request.method == 'POST':
        title = request.form.get('title')
        content = request.form.get('content')
        # 获取关联的商品ID (如果是 'none' 或者空，则为 None)
        product_id_str = request.form.get('product_id')

        # 🔥 图片上传处理逻辑
        image_url = None
        if 'image_file' in request.files:
            file = request.files['image_file']
            if file and file.filename != '' and allowed_file(file.filename):
                # 使用时间戳和安全文件名防止重复
                filename = secure_filename(file.filename)
                unique_filename = f"{int(time.time())}_{filename}"
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
                file.save(filepath)
                # 构造图片的 URL 路径
                image_url = url_for('static', filename=f'uploads/{unique_filename}')
        # 🔥 结束图片上传处理逻辑

        related_product_id = int(product_id_str) if product_id_str and product_id_str != 'none' else None

        if not title or not content:
            flash('标题和内容不能为空！', 'error')
        else:
            try:
                new_post = CommunityPost(
                    user_id=session['user_id'],
                    title=title,
                    content=content,
                    related_product_id=related_product_id,
                    image_url=image_url  # <-- 存储图片 URL
                )
                db.session.add(new_post)
                db.session.commit()
                flash('🎉 帖子发布成功！')
                return redirect(url_for('community'))
            except Exception as e:
                db.session.rollback()
                flash(f'发布失败: {e}', 'error')

    # GET 请求：如果是农户，获取他的商品列表
    my_products = []
    if user and user.role == 1:
        # 仅获取已上架的商品供关联
        my_products = Product.query.filter_by(farmer_id=user.user_id, is_on_sale=True).options(
            joinedload(Product.skus)).all()

    return render_template('publish_post.html', my_products=my_products)


# ==========================================
# 🛒 购物车功能
# ==========================================

@app.route('/cart')
def view_cart():
    """查看购物车页面"""
    if 'user_id' not in session:
        flash('请先登录以查看购物车。')
        return redirect(url_for('login'))

    user_id = session['user_id']  # <-- 获取 user_id

    # 优化：预加载 sku 及其 product 和 farmer
    cart_items = CartItem.query.filter_by(user_id=user_id).options(
        joinedload(CartItem.sku).joinedload(ProductSKU.product).joinedload(Product.farmer)
    ).all()

    total_price = sum(item.sku.price * item.quantity for item in cart_items)

    # 🔥 新增: 获取推荐商品
    recommendations = get_formatted_recommendations(user_id)

    return render_template('cart.html', cart_items=cart_items, total_price=total_price,
                           recommendations=recommendations)  # <-- 传递 recommendations


@app.route('/cart/add/<int:sku_id>', methods=['POST'])
def add_to_cart(sku_id):
    """添加 SKU 到购物车"""
    if 'user_id' not in session:
        flash('请先登录才能添加商品到购物车。')
        return redirect(url_for('login'))

    user_id = session['user_id']
    quantity = int(request.form.get('quantity', 1))

    sku = ProductSKU.query.get_or_404(sku_id)

    if quantity <= 0:
        flash('数量必须大于零。', 'error')
        return redirect(url_for('product_detail', product_id=sku.product_id))

    cart_item = CartItem.query.filter_by(user_id=user_id, sku_id=sku_id).first()

    try:
        current_in_cart = cart_item.quantity if cart_item else 0
        if sku.stock < quantity + current_in_cart:
            flash(f'⚠️ 库存不足，当前库存为 {sku.stock}。', 'error')
            return redirect(url_for('product_detail', product_id=sku.product_id))

        if cart_item:
            cart_item.quantity += quantity
        else:
            new_cart_item = CartItem(
                user_id=user_id,
                sku_id=sku_id,
                quantity=quantity
            )
            db.session.add(new_cart_item)

        db.session.commit()

        product_id = sku.product_id
        new_log = BehaviorLog(user_id=user_id, product_id=product_id, behavior_type=3)  # 加购行为
        db.session.add(new_log)
        db.session.commit()

        flash('✅ 商品已成功加入购物车！')
    except Exception as e:
        db.session.rollback()
        flash(f'❌ 加入购物车失败: {e}', 'error')

    return redirect(url_for('view_cart'))


@app.route('/cart/update', methods=['POST'])
def update_cart():
    """更新购物车中商品的数量"""
    if 'user_id' not in session:
        return redirect(url_for('login'))

    cart_item_id = request.form.get('item_id', type=int)
    new_quantity = request.form.get('quantity', type=int)

    cart_item = CartItem.query.filter_by(id=cart_item_id, user_id=session['user_id']).first()

    if cart_item and new_quantity is not None:
        if new_quantity > 0:
            if new_quantity > cart_item.sku.stock:
                flash(f'⚠️ 数量不能超过库存 ({cart_item.sku.stock})。', 'error')
            else:
                cart_item.quantity = new_quantity
                db.session.commit()
        elif new_quantity == 0:
            db.session.delete(cart_item)
            db.session.commit()
            flash('商品已从购物车移除。')
        else:
            flash('数量无效。')

    return redirect(url_for('view_cart'))


@app.route('/cart/remove/<int:item_id>')
def remove_from_cart(item_id):
    """从购物车移除单个商品"""
    if 'user_id' not in session:
        return redirect(url_for('login'))

    cart_item = CartItem.query.filter_by(id=item_id, user_id=session['user_id']).first()

    if cart_item:
        db.session.delete(cart_item)
        db.session.commit()
        flash('商品已成功从购物车移除。')

    return redirect(url_for('view_cart'))


# ==========================================
# 💵 结算与下单
# ==========================================

@app.route('/checkout', methods=['GET', 'POST'])
def checkout():
    """结算页面：展示商品、运费和收货信息"""
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']

    # 默认查询所有项目 (用于 GET 访问)
    cart_items_query = CartItem.query.filter_by(user_id=user_id).options(
        joinedload(CartItem.sku).joinedload(ProductSKU.product).joinedload(Product.farmer)
    )

    # 如果是 POST 请求，则根据前端选中的商品 ID 进行严格过滤
    if request.method == 'POST':
        selected_items_str = request.form.get('selected_items', '')

        try:
            # 1. 解析选中的 CartItem ID 列表
            selected_ids = [int(id_str) for id_str in selected_items_str.split(',') if id_str.strip()]
        except ValueError:
            flash("结算错误：商品选择列表格式不正确。", 'error')
            return redirect(url_for('view_cart'))

        if not selected_ids:
            flash("请至少选择一件商品进行结算。", 'error')
            return redirect(url_for('view_cart'))

        # 2. 严格筛选出用户选中的商品
        cart_items = cart_items_query.filter(CartItem.id.in_(selected_ids)).all()

    else:
        # GET 请求或未进行 POST 提交，使用默认查询（通常会拉取所有购物车商品）
        cart_items = cart_items_query.all()

    if not cart_items:
        flash("购物车为空，无法结算。", 'error')
        return redirect(url_for('index'))

    total_price = sum(item.sku.price * item.quantity for item in cart_items)

    # **修复运费计算：传入商品列表，让函数根据模板 ID 计算最高运费**
    shipping_cost = calculate_shipping_cost(cart_items, None)

    final_total = total_price + shipping_cost

    # --- Start Fix 1: Pass the list of selected item IDs to the template ---
    # 收集当前结算的 cart_item ID 列表，用于传递给 place_order
    selected_cart_item_ids = [item.id for item in cart_items]

    return render_template('checkout.html',
                           cart_items=cart_items,
                           total_price=total_price,
                           shipping_cost=shipping_cost,
                           final_total=final_total,
                           selected_item_ids=selected_cart_item_ids) # <-- 新增变量
    # --- End Fix 1 ---


@app.route('/place_order', methods=['POST'])
def place_order():
    """最终下单并记录购买行为 (type=4)"""
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']

    # 1. 获取收货信息和总额
    receiver_name = request.form.get('receiver_name')
    receiver_phone = request.form.get('receiver_phone')
    address = request.form.get('address')

    total_amount_str = request.form.get('final_total')
    # --- Start Fix 2: Retrieve selected item IDs and filter the cart query ---
    # 接收 checkout 页面传来的选中商品 ID 列表 (对应 checkout.html 中新增的字段)
    selected_items_str = request.form.get('selected_items_to_buy', '')

    if not total_amount_str:
        flash("订单无效: 缺少总金额信息。", 'error')
        return redirect(url_for('checkout'))

    try:
        # 1. 解析选中的 CartItem ID 列表
        selected_ids = [int(id_str) for id_str in selected_items_str.split(',') if id_str.strip()]
    except ValueError:
        flash("订单无效: 商品选择列表格式不正确。", 'error')
        return redirect(url_for('checkout'))

    if not selected_ids:
        flash("订单无效: 缺少购买商品信息。", 'error')
        return redirect(url_for('view_cart'))

    # 优化：预加载 sku 及其 product
    # BUG FIX: 仅检索用户选中的商品 ID
    cart_items = CartItem.query.filter(CartItem.user_id == user_id, CartItem.id.in_(selected_ids)).options(
        joinedload(CartItem.sku).joinedload(ProductSKU.product)
    ).all()
    # --- End Fix 2 ---

    if not cart_items:
        # 如果选中的商品已经不存在了，也视为错误
        flash("订单无效: 购物车为空或选中商品已失效。", 'error')
        return redirect(url_for('view_cart'))

    try:
        total_amount_from_form = Decimal(total_amount_str)
    except Exception:
        flash("订单无效: 金额格式错误。", 'error')
        return redirect(url_for('checkout'))

    if total_amount_from_form <= Decimal('0.00'):
        flash("订单无效: 金额必须大于零。", 'error')
        return redirect(url_for('checkout'))

    # FIX: goods_total 和 shipping_cost 现在是基于正确的 cart_items 列表计算的
    goods_total = sum(item.sku.price * item.quantity for item in cart_items)

    # **修复运费计算：传入 cart_items 列表，以便根据模板计算**
    shipping_cost = calculate_shipping_cost(cart_items, None)

    calculated_total = goods_total + shipping_cost

    if abs(calculated_total - total_amount_from_form) > Decimal('0.01'):
        flash("❌ 订单金额校验失败，请重新结算。", 'error')
        return redirect(url_for('checkout'))

    try:
        # 2. 创建订单主表记录
        # status=2 (待发货) 表示已支付，简化了支付流程
        new_order = Order(
            user_id=user_id,
            total_amount=calculated_total,
            shipping_cost=shipping_cost,
            receiver_name=receiver_name,
            receiver_phone=receiver_phone,
            address=address,
            status=2
        )
        db.session.add(new_order)
        db.session.flush()

        # 3. 遍历购物车，创建订单详情记录，并记录购买行为
        # FIX: Loop over the correct cart_items list (already filtered by selected_ids)
        for item in cart_items:
            sku = item.sku

            # 检查库存 (SKU 级别)，并使用 with_for_update 锁定库存行（防止超卖）
            sku_lock = ProductSKU.query.filter_by(sku_id=sku.sku_id).with_for_update().first()

            if sku_lock.stock < item.quantity:
                raise ValueError(f"商品 {sku_lock.product.name} ({sku_lock.spec_name}) 库存不足。")

            product = sku_lock.product

            # 创建订单详情项 (基于 SKU)
            new_order_item = OrderItem(
                order_id=new_order.order_id,
                sku_id=sku_lock.sku_id,
                product_id=product.product_id,
                product_name=product.name,
                farmer_id=product.farmer_id,
                quantity=item.quantity,
                price=sku_lock.price
            )
            db.session.add(new_order_item)

            # 扣减库存 (SKU 级别)
            sku_lock.stock -= item.quantity

            # 记录购买行为 (BehaviorLog 仍然使用 Product ID)
            new_log = BehaviorLog(user_id=user_id, product_id=product.product_id, behavior_type=4)
            db.session.add(new_log)

        # 4. 清空购物车
        # FIX: 必须只删除本次购买的商品，而不是清空整个购物车
        items_to_delete_ids = [item.id for item in cart_items]
        # 使用 filter 和 synchronize_session='fetch' 来确保正确删除
        CartItem.query.filter(CartItem.user_id == user_id, CartItem.id.in_(items_to_delete_ids)).delete(synchronize_session='fetch')

        # 5. 提交所有更改
        db.session.commit()
        flash(f'🎉 订单 #{new_order.order_id} 提交成功，请耐心等待发货！', 'success')
        return redirect(url_for('orders'))

    except ValueError as e:
        db.session.rollback()
        flash(f'❌ 下单失败: {e}', 'error')
        return redirect(url_for('checkout'))
    except Exception as e:
        db.session.rollback()
        print(f"致命下单错误: {e}")
        flash(f'❌ 下单失败，系统错误。', 'error')
        return redirect(url_for('checkout'))


# ==========================================
# 👤 用户中心与发布功能
# ==========================================

@app.route('/profile')
def profile():
    if 'user_id' not in session: return redirect(url_for('login'))
    user = db.session.get(User, session['user_id'])
    user_id = session['user_id']  # <-- 获取 user_id

    pending_orders_count = 0
    total_revenue = Decimal('0.00')

    if user and user.role == 1:
        # 农户：查询待发货订单数
        order_ids_with_farmer_items = select(OrderItem.order_id).where(
            OrderItem.farmer_id == user.user_id
        ).distinct()

        # 计算待发货订单总数，必须是待发货状态 (status=2)
        pending_orders_count = db.session.query(Order).filter(
            Order.order_id.in_(order_ids_with_farmer_items),
            Order.status == 2
        ).count()

        # 🔥 修复：计算累计收益 (仅计算已完成 status=4 的订单项)
        sales_stats = db.session.query(
            func.sum(OrderItem.price * OrderItem.quantity).label('total_revenue')
        ).filter(
            OrderItem.farmer_id == user.user_id,
            # 确保只计算已完成 (status=4) 的订单
            OrderItem.order.has(Order.status == 4)
        ).first()

        total_revenue = sales_stats.total_revenue if sales_stats and sales_stats.total_revenue is not None else Decimal(
            '0.00')

        recommendations = []  # 农户页面不展示推荐

    else:
        # 🔥 新增: 仅为消费者角色获取推荐商品
        recommendations = get_formatted_recommendations(user_id)

    # 修改返回参数，传递 total_revenue
    return render_template('profile.html', user=user, pending_orders_count=pending_orders_count,
                           total_revenue=total_revenue, recommendations=recommendations)  # <-- 传递 recommendations


@app.route('/profile/edit', methods=['GET', 'POST'])
def edit_profile():
    if 'user_id' not in session: return redirect(url_for('login'))
    user = db.session.get(User, session['user_id'])

    if request.method == 'POST':
        user.email = request.form.get('email')
        user.phone = request.form.get('phone')

        if user.role == 1:
            info = db.session.get(FarmerInfo, user.user_id)
            if not info:
                info = FarmerInfo(farmer_id=user.user_id)
                db.session.add(info)
            info.shop_name = request.form.get('shop_name')
            info.contact_person = request.form.get('contact_person')
            info.farm_address = request.form.get('farm_address')
            info.bio = request.form.get('bio')

        db.session.commit()
        flash('✅ 资料修改成功！')
        return redirect(url_for('profile'))

    return render_template('edit_profile.html', user=user)


@app.route('/product/publish', methods=['GET', 'POST'])
def publish_product():
    """农户发布商品 (支持图片上传)"""
    if 'user_id' not in session: return redirect(url_for('login'))
    user = db.session.get(User, session['user_id'])

    if not user or user.role != 1 or user.status != 1:
        flash('❌ 无权发布，请等待审核。')
        return redirect(url_for('profile'))

    if request.method == 'POST':
        try:
            # 1. 处理图片上传
            image_url = None
            if 'image_file' in request.files:
                file = request.files['image_file']
                if file and file.filename != '' and allowed_file(file.filename):
                    # 🔥 使用时间戳和安全文件名防止重复
                    filename = secure_filename(file.filename)
                    unique_filename = f"{int(time.time())}_{filename}"
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_filename))
                    image_url = url_for('static', filename='uploads/' + unique_filename)

            # 2. 如果没上传文件，尝试使用输入的 URL
            if not image_url:
                image_url = request.form.get('image_url')

            spec_name = request.form.get('spec_name')
            price = request.form.get('price', type=float)
            stock = request.form.get('stock', type=int)
            shipping_template_id = request.form.get('shipping_template_id', type=int)

            if not price or price <= 0 or stock is None or stock < 0 or not spec_name:
                raise ValueError("必须提供有效价格、库存和规格名称。")

            # 3. 保存商品主表 (Product)
            new_product = Product(
                farmer_id=user.user_id,
                name=request.form.get('name'),
                category=request.form.get('category'),
                origin=request.form.get('origin'),
                description=request.form.get('description'),
                image_url=image_url,
                shipping_template_id=shipping_template_id
            )
            db.session.add(new_product)
            db.session.flush()

            # 创建默认 SKU
            new_sku = ProductSKU(
                product_id=new_product.product_id,
                spec_name=spec_name,
                # 确保价格是 Decimal 类型
                price=Decimal(str(price)),
                stock=stock
            )
            db.session.add(new_sku)

            db.session.commit()
            flash('🎉 发布成功！')
            return redirect(url_for('profile'))
        except Exception as e:
            db.session.rollback()
            print(f"发布错误: {e}")
            flash(f'❌ 发布失败: {e}', 'error')
            return redirect(url_for('publish_product'))

    return render_template('publish.html')


@app.route('/farmer/product/<int:product_id>/toggle_sale')
def toggle_product_status(product_id):
    """切换商品的上架/下架状态"""
    if 'user_id' not in session: return redirect(url_for('login'))

    current_user = db.session.get(User, session['user_id'])
    product = Product.query.get_or_404(product_id)

    if current_user.role != 1 or product.farmer_id != current_user.user_id:
        flash('🚫 权限不足，无法操作该商品。')
        return redirect(url_for('profile'))

    product.is_on_sale = not product.is_on_sale
    db.session.commit()

    status_msg = "上架" if product.is_on_sale else "下架"
    flash(f'✅ 商品 **{product.name}** 已成功切换为 **{status_msg}** 状态！')

    return redirect(url_for('profile'))


@app.route('/product/edit/<int:product_id>', methods=['GET', 'POST'])
def edit_product(product_id):
    """农户修改已发布的商品信息和规格"""
    if 'user_id' not in session: return redirect(url_for('login'))
    user = db.session.get(User, session['user_id'])

    # 预加载 SKU
    product = Product.query.options(joinedload(Product.skus)).get_or_404(product_id)

    if not user or user.role != 1 or product.farmer_id != user.user_id:
        flash('🚫 权限不足，无法编辑该商品。')
        return redirect(url_for('profile'))

    if request.method == 'POST':
        try:
            # --- 1. 获取并处理 SKU 列表数据 ---
            sku_ids = request.form.getlist('sku_id[]')
            spec_names = request.form.getlist('spec_name[]')
            prices = request.form.getlist('price[]')
            stocks = request.form.getlist('stock[]')

            # --- DEBUG LOG: 打印接收到的原始 SKU 数组 ---
            print("\n--- DEBUG: RECEIVED SKU DATA ---")
            print(f"SKU IDs: {sku_ids}")
            print(f"Spec Names: {spec_names}")
            print(f"Prices: {prices}")
            print(f"Stocks: {stocks}")
            print("----------------------------------\n")
            # -----------------------------------------

            if not spec_names or len(spec_names) == 0:
                raise ValueError("必须至少保留一个商品规格。")

            # 映射现有 SKU 以便查找和更新
            existing_skus = {sku.sku_id: sku for sku in product.skus}
            submitted_sku_ids = set()

            # --- 2. 校验、更新或创建 SKU ---
            if len(sku_ids) != len(spec_names) or len(prices) != len(spec_names) or len(stocks) != len(spec_names):
                raise ValueError("提交的规格数据数量不匹配。")

            for i in range(len(spec_names)):

                sku_id_str = sku_ids[i].strip()
                spec_name_val = spec_names[i].strip()
                price_str = prices[i].strip()
                stock_str = stocks[i].strip()

                # --- 核心修复：跳过完全空白或仅 stock 为 '0' 的新增行 ---
                # 只有当它是新增行 (无 ID)，且 名称、价格、库存（'','0'）都为空时才跳过。
                if not sku_id_str and not spec_name_val and not price_str and (stock_str == '0' or not stock_str):
                    continue
                # ------------------------------------

                # 处理空字符串，确保能转为 Decimal/int
                price_val = Decimal(price_str or '0')
                stock_val = int(stock_str or '0')

                # 严格校验：名称必须存在，价格必须大于零，库存不能为负数
                if not spec_name_val or price_val <= Decimal('0.00') or stock_val < 0:
                    # 校验失败时，抛出包含当前索引的详细错误信息
                    raise ValueError(f"规格 '{spec_name_val or '[名称为空]'}' 校验失败：价格必须大于零，库存不能为负数，且名称不能为空。")

                sku_id_str = sku_ids[i].strip()

                if sku_id_str:
                    # 现有 SKU：更新
                    sku_id = int(sku_id_str)
                    current_sku = existing_skus.get(sku_id)
                    if not current_sku:
                        raise Exception(f"尝试更新不存在的 SKU ID: {sku_id}")

                    current_sku.spec_name = spec_names[i]
                    current_sku.price = price_val
                    current_sku.stock = stock_val
                    submitted_sku_ids.add(sku_id)
                else:
                    # 新 SKU：创建
                    new_sku = ProductSKU(product_id=product_id, spec_name=spec_names[i], price=price_val,
                                         stock=stock_val)
                    db.session.add(new_sku)

            # --- 3. 处理 SKU 删除 (删除提交中缺失的现有 SKU) ---
            for sku_id, sku_obj in existing_skus.items():
                if sku_id not in submitted_sku_ids:
                    # 如果现有 SKU 不在本次提交的列表中，则删除它
                    db.session.delete(sku_obj)

            # --- 4. 图片和基础信息处理 (与之前保持一致) ---
            image_url = product.image_url

            if 'image_file' in request.files:
                file = request.files['image_file']
                if file and file.filename != '' and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    unique_filename = f"{int(time.time())}_{filename}"
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_filename))
                    image_url = url_for('static', filename='uploads/' + unique_filename)

            manual_url_input = request.form.get('image_url')

            if not 'image_file' in request.files or not request.files['image_file'].filename:
                # 只有当用户没有上传新文件时，才考虑手动输入的 URL
                if manual_url_input:
                    image_url = manual_url_input
                else:
                    # 如果手动输入为空，且没有上传文件，则清空 URL
                    image_url = None

            # 5. 更新 Product 主表字段
            product.name = request.form.get('name')
            product.category = request.form.get('category')
            product.origin = request.form.get('origin')
            product.description = request.form.get('description')
            product.image_url = image_url
            product.shipping_template_id = request.form.get('shipping_template_id', type=int)

            db.session.commit()
            flash('✅ 商品信息修改成功！')
            return redirect(url_for('profile'))

        except ValueError as ve:
            db.session.rollback()
            flash(f'❌ 数据校验失败: {ve}', 'error')
            # 发生校验错误时，使用重定向（PRG模式）
            return redirect(url_for('edit_product', product_id=product_id))

        except Exception as e:
            db.session.rollback()
            print(f"编辑错误: {e}")
            flash(f'❌ 编辑失败，系统错误: {e}', 'error')
            # 发生严重错误时，使用重定向（PRG模式）
            return redirect(url_for('edit_product', product_id=product_id))

    # GET request: Ensure product.skus is available for the template
    return render_template('edit_product.html', product=product, skus=product.skus)


@app.route('/product/delete/<int:product_id>', methods=['POST'])
def delete_product(product_id):
    """农户删除商品及其所有关联数据"""
    if 'user_id' not in session: return redirect(url_for('login'))
    current_user = db.session.get(User, session['user_id'])

    # 查找商品，确保存在
    product = Product.query.get_or_404(product_id)

    if not current_user or current_user.role != 1 or product.farmer_id != current_user.user_id:
        flash('🚫 权限不足，无法删除该商品。')
        return redirect(url_for('profile'))

    try:
        # 必须先删除所有关联的外键数据，才能删除主商品
        # 1. 删除所有 SKU
        ProductSKU.query.filter_by(product_id=product_id).delete()
        # 2. 删除所有购物车项
        CartItem.query.join(ProductSKU, CartItem.sku_id == ProductSKU.sku_id).filter(
            ProductSKU.product_id == product_id).delete(synchronize_session=False)
        # 3. 删除所有关联社区帖子
        CommunityPost.query.filter_by(related_product_id=product_id).delete()
        # 4. 删除所有行为日志 (CF数据源)
        BehaviorLog.query.filter_by(product_id=product_id).delete()
        # 5. 删除所有相似度记录 (CF结果)
        ItemSimilarity.query.filter(
            (ItemSimilarity.item_a_id == product_id) | (ItemSimilarity.item_b_id == product_id)
        ).delete()
        # 🔥 新增：删除所有商品评价
        ProductReview.query.filter_by(product_id=product_id).delete(synchronize_session=False)

        # 注意：OrderItem 不应删除，因为那是历史订单记录。

        # 6. 删除商品主表
        db.session.delete(product)
        db.session.commit()

        flash(f'✅ 商品 **{product.name}** 及其所有关联数据已彻底删除。')

    except Exception as e:
        db.session.rollback()
        print(f"删除商品错误: {e}")
        flash('❌ 删除商品失败，可能存在未清理的订单关联数据。')

    return redirect(url_for('profile'))


@app.route('/order/<int:order_id>/review', methods=['POST'])
def submit_review(order_id):
    """消费者：对已完成订单提交评价 (1-5星)"""
    if 'user_id' not in session: return redirect(url_for('login'))
    user_id = session['user_id']

    order = Order.query.filter_by(order_id=order_id, user_id=user_id).first_or_404()

    # 1. 状态检查：必须是已完成的订单才能评价
    if order.status != 4:
        flash('⚠️ 订单未完成，无法评价。')
        return redirect(url_for('order_detail', order_id=order_id))

    # 2. 检查是否已评价（虽然数据库约束已包含，但此为前端二次校验）
    existing_review = ProductReview.query.filter_by(order_id=order_id).first()
    if existing_review:
        flash('⚠️ 您已评价过此订单。', 'error')
        return redirect(url_for('order_detail', order_id=order_id))

    try:
        rating = int(request.form.get('rating'))
        content = request.form.get('content')

        if not (1 <= rating <= 5):
            flash('评分必须在 1 到 5 星之间。', 'error')
            return redirect(url_for('order_detail', order_id=order_id))

        # 3. 确定评分目标商品 ID (简化：取订单中第一个商品作为评分目标)
        # 注意：由于订单可能包含多个农户的商品，这里简化为只对第一个商品进行评价。
        # 更好的做法是让用户选择评价哪个商品。但按照当前模型结构，我们只能关联到 Product ID。
        first_order_item = OrderItem.query.filter_by(order_id=order_id).first()
        if not first_order_item:
            flash('❌ 订单中没有商品，无法评价。', 'error')
            return redirect(url_for('order_detail', order_id=order_id))

        product_id_to_rate = first_order_item.product_id

        # 4. 创建新的评价记录
        new_review = ProductReview(
            user_id=user_id,
            order_id=order_id,
            product_id=product_id_to_rate,
            rating=rating,
            content=content
        )
        db.session.add(new_review)
        db.session.commit()

        flash('✅ 评价提交成功！感谢您的反馈。')

    except Exception as e:
        db.session.rollback()
        # 尝试捕获唯一约束错误
        if 'IntegrityError' in str(e):
            flash('⚠️ 您已评价过此订单。', 'error')
        else:
            flash(f'❌ 评价提交失败: {e}', 'error')

    return redirect(url_for('order_detail', order_id=order_id))


# ----------------- 消费者订单管理 (Consumer Order Management) -----------------

@app.route('/profile/orders')
def orders():
    """消费者：查看自己的订单列表"""
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']  # <-- 获取 user_id

    # 预加载 items 及其 sku 和 product
    user_orders = Order.query.filter_by(user_id=user_id).options(
        joinedload(Order.items).joinedload(OrderItem.sku).joinedload(ProductSKU.product)
    ).order_by(Order.order_date.desc()).all()

    status_map = {
        1: '待支付',
        2: '待发货',
        3: '待收货',
        4: '已完成',
        5: '已取消',
        6: '售后中',
        7: '已退款/售后完成'
    }

    # 🔥 新增: 获取推荐商品
    recommendations = get_formatted_recommendations(user_id)

    return render_template('orders.html', orders=user_orders, status_map=status_map,
                           recommendations=recommendations)  # <-- 传递 recommendations


@app.route('/order/<int:order_id>')
def order_detail(order_id):
    """订单详情页：展示订单内的商品、收货信息等（支持消费者、农户、管理员查看）"""
    if 'user_id' not in session:
        return redirect(url_for('login'))

    current_user = db.session.get(User, session['user_id'])

    # 1. 查找特定订单
    # 预加载 items 及其 sku 和 product
    order = Order.query.options(
        joinedload(Order.items).joinedload(OrderItem.sku).joinedload(ProductSKU.product)
    ).get_or_404(order_id)

    # 2. 权限检查逻辑：允许消费者、负责农户和管理员访问
    has_access = False

    if order.user_id == current_user.user_id:
        has_access = True

    elif current_user.role in [1, 2]:
        if current_user.role == 2:
            has_access = True

        else:
            is_responsible = OrderItem.query.filter_by(order_id=order_id, farmer_id=current_user.user_id).first()
            if is_responsible:
                has_access = True

    if not has_access:
        flash('🚫 权限不足，无法查看该订单详情。', 'error')
        if current_user.role == 1:
            return redirect(url_for('farmer_orders'))
        return redirect(url_for('profile'))

    # 订单状态映射 (保持最新状态)
    status_map = {
        1: '待支付', 2: '待发货', 3: '待收货', 4: '已完成', 5: '已取消',
        6: '售后中', 7: '已退款/售后完成'
    }

    order.status_map = status_map

    # 🔥 新增：检查是否已评价
    has_reviewed = None
    if current_user:
        has_reviewed = ProductReview.query.filter_by(order_id=order_id, user_id=current_user.user_id).first()

    return render_template('order_detail.html', order=order, status_map=status_map, has_reviewed=has_reviewed)


@app.route('/order/<int:order_id>/confirm_receipt', methods=['POST'])
def confirm_receipt(order_id):
    """消费者：将订单状态从“待收货”(3)改为“已完成”(4)"""
    if 'user_id' not in session: return redirect(url_for('login'))
    user_id = session['user_id']

    order = Order.query.filter_by(order_id=order_id, user_id=user_id).first_or_404()

    if order.status != 3:
        flash('⚠️ 订单状态不是待收货，无法确认。')
        return redirect(url_for('order_detail', order_id=order_id))

    try:
        order.status = 4
        db.session.commit()
        # 记录行为日志: 购买
        for item in order.items:
            new_log = BehaviorLog(user_id=user_id, product_id=item.product_id, behavior_type=4)
            db.session.add(new_log)
        db.session.commit()

        flash(f'🎉 订单 #{order_id} 确认收货成功，交易完成！', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'❌ 操作失败: {e}', 'error')

    return redirect(url_for('order_detail', order_id=order_id))


@app.route('/order/<int:order_id>/edit_address', methods=['GET', 'POST'])
def edit_order_address(order_id):
    """
    修改订单地址，仅限发货前（状态 1 或 2）
    权限逻辑：允许消费者和负责农户/管理员访问。
    """
    if 'user_id' not in session: return redirect(url_for('login'))
    current_user = db.session.get(User, session['user_id'])

    # 1. 查找订单
    order = Order.query.get_or_404(order_id)

    # 订单状态映射，用于模板显示
    status_map = {
        1: '待支付', 2: '待发货', 3: '待收货', 4: '已完成', 5: '已取消',
        6: '售后中', 7: '已退款/售后完成'
    }

    # 2. 权限检查逻辑：允许消费者、负责农户、或管理员
    has_access = False

    if order.user_id == current_user.user_id:
        has_access = True

    elif current_user.role in [1, 2]:
        if current_user.role == 2:
            has_access = True

        else:
            is_responsible = OrderItem.query.filter_by(order_id=order_id, farmer_id=current_user.user_id).first()
            if is_responsible:
                has_access = True

    if not has_access:
        flash('🚫 权限不足，无法修改该订单地址。', 'error')
        if current_user.role == 1:
            return redirect(url_for('farmer_orders'))
        return redirect(url_for('profile'))

    # 3. 状态检查：订单必须是 1 (待支付) 或 2 (待发货)
    if order.status != 1 and order.status != 2:
        flash('⚠️ 订单已发货或已处理，无法修改收货地址。')
        return redirect(url_for('order_detail', order_id=order_id))

    if request.method == 'POST':
        new_address = request.form.get('address')
        new_receiver_name = request.form.get('receiver_name')
        new_receiver_phone = request.form.get('receiver_phone')

        if not all([new_address, new_receiver_name, new_receiver_phone]):
            flash('收货信息不能为空。', 'error')
            order.status_map = status_map
            return render_template('edit_order_address.html', order=order, status_map=status_map)

        try:
            order.address = new_address
            order.receiver_name = new_receiver_name
            order.receiver_phone = new_receiver_phone
            db.session.commit()
            flash('✅ 收货地址修改成功！')
        except Exception as e:
            db.session.rollback()
            flash(f'❌ 地址修改失败: {e}', 'error')

        return redirect(url_for('order_detail', order_id=order_id))

    order.status_map = status_map
    return render_template('edit_order_address.html', order=order, status_map=status_map)


@app.route('/order/<int:order_id>/after_sales', methods=['POST'])
def apply_for_after_sales(order_id):
    """消费者：将订单状态从“已完成”(4)改为“售后中”(6)"""
    if 'user_id' not in session: return redirect(url_for('login'))
    user_id = session['user_id']

    order = Order.query.filter_by(order_id=order_id, user_id=user_id).first_or_404()

    if order.status != 4:
        flash('⚠️ 订单未完成，无法申请售后。')
        return redirect(url_for('order_detail', order_id=order_id))

    if not request.form.get('after_sales_reason'):
        flash('⚠️ 请填写售后原因。', 'error')
        return redirect(url_for('order_detail', order_id=order_id))

    try:
        order.status = 6
        order.after_sales_reason = request.form.get('after_sales_reason')
        db.session.commit()
        flash(f'📢 订单 #{order_id} 已成功提交售后申请，请等待商家处理。', 'warning')
    except Exception as e:
        db.session.rollback()
        flash(f'❌ 售后申请失败: {e}', 'error')

    return redirect(url_for('order_detail', order_id=order_id))


@app.route('/farmer/orders', defaults={'status_filter': 'pending'})
@app.route('/farmer/orders/<status_filter>')
def farmer_orders(status_filter):
    """
    农户：查看订单列表 (支持按状态过滤)
    status_filter: pending (待发货, status=2), shipped (已发货/待收货, status=3),
                   aftersales (售后中, status=6), completed (已完成/售后结束, status=4/7)
    """
    if 'user_id' not in session: return redirect(url_for('login'))
    user = db.session.get(User, session['user_id'])

    if not user or user.role != 1:
        flash('🚫 权限不足')
        return redirect(url_for('index'))

    status_map = {
        1: '待支付', 2: '待发货', 3: '待收货', 4: '已完成', 5: '已取消',
        6: '售后中', 7: '已退款/售后完成'
    }

    # 1. 确定需要查询的订单状态
    if status_filter == 'pending':
        target_statuses = [2]
        title = "待发货订单"
    elif status_filter == 'shipped':
        target_statuses = [3]
        title = "已发货订单"
    elif status_filter == 'completed':
        target_statuses = [4, 7]
        title = "已完成/售后结束订单"
    elif status_filter == 'aftersales':
        target_statuses = [6]
        title = "售后中订单"
    else:
        target_statuses = [2]
        title = "待发货订单"
        status_filter = 'pending'

    # 2. 查找包含该农户商品的订单ID
    order_ids = db.session.query(OrderItem.order_id).filter(
        OrderItem.farmer_id == user.user_id
    ).distinct().all()

    unique_order_ids = [o[0] for o in order_ids]

    # 3. 根据订单ID查询订单主表，并过滤状态
    farmer_orders = Order.query.filter(
        Order.order_id.in_(unique_order_ids),
        Order.status.in_(target_statuses)
    ).order_by(Order.order_date.desc()).all()

    # 4. 统计所有状态的数量（用于顶部导航）
    all_orders = Order.query.filter(Order.order_id.in_(unique_order_ids)).all()
    count_map = {
        'pending': sum(1 for o in all_orders if o.status == 2),
        'shipped': sum(1 for o in all_orders if o.status == 3),
        'completed': sum(1 for o in all_orders if o.status == 4 or o.status == 7),
        'aftersales': sum(1 for o in all_orders if o.status == 6)
    }

    return render_template('farmer_orders.html',
                           orders=farmer_orders,
                           status_map=status_map,
                           current_filter=status_filter,
                           title=title,
                           count_map=count_map)


@app.route('/farmer/order/<int:order_id>/ship', methods=['POST'])
def ship_order(order_id):
    """农户：将订单状态从“待发货”(2)改为“待收货”(3)，接收发货单号"""
    if 'user_id' not in session: return redirect(url_for('login'))
    user = db.session.get(User, session['user_id'])

    if not user or user.role != 1:
        flash('🚫 权限不足')
        return redirect(url_for('index'))

    if not request.form.get('tracking_number'):
        flash('⚠️ 必须填写发货单号。', 'error')
        return redirect(url_for('farmer_orders'))

    order = Order.query.get_or_404(order_id)

    if order.status != 2:
        flash(f'⚠️ 订单 #{order_id} 状态不是待发货，无法操作。')
        return redirect(url_for('farmer_orders'))

    is_responsible = OrderItem.query.filter_by(order_id=order_id, farmer_id=user.user_id).first()

    if not is_responsible:
        flash(f'🚫 订单 #{order_id} 不包含您的商品，无权操作。')
        return redirect(url_for('farmer_orders'))

    try:
        order.status = 3
        order.tracking_number = request.form.get('tracking_number')
        db.session.commit()
        flash(f'✅ 订单 #{order_id} 已成功发货 (单号: {request.form.get("tracking_number")})！状态更新为“待收货”。', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'❌ 订单发货失败: {e}', 'error')

    return redirect(url_for('farmer_orders', status_filter='shipped'))


@app.route('/farmer/order/<int:order_id>/handle_after_sales', methods=['POST'])
def handle_after_sales(order_id):
    """农户：处理售后申请，将订单状态从“售后中”(6)改为“已退款/售后完成”(7)"""
    if 'user_id' not in session: return redirect(url_for('login'))
    user = db.session.get(User, session['user_id'])

    if not user or user.role != 1:
        flash('🚫 权限不足')
        return redirect(url_for('index'))

    order = Order.query.get_or_404(order_id)

    if order.status != 6:
        flash(f'⚠️ 订单 #{order_id} 状态不是售后中，无法处理。')
        return redirect(url_for('farmer_orders', status_filter='aftersales'))

    is_responsible = OrderItem.query.filter_by(order_id=order_id, farmer_id=user.user_id).first()

    if not is_responsible:
        flash(f'🚫 订单 #{order_id} 不包含您的商品，无权操作。')
        return redirect(url_for('farmer_orders'))

    try:
        order.status = 7
        db.session.commit()
        flash(f'✅ 订单 #{order_id} 售后处理完成，状态更新为“已退款/售后完成”。', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'❌ 售后处理失败: {e}', 'error')

    return redirect(url_for('farmer_orders', status_filter='completed'))


@app.route('/profile/favorites')
def view_favorites():
    """查看我的收藏列表"""
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']  # <-- 获取 user_id

    # 1. 查询该用户所有 behavior_type=2 (收藏) 的日志
    # 按时间倒序排列，最近收藏的在前面
    logs = BehaviorLog.query.filter_by(
        user_id=user_id,
        behavior_type=2
    ).order_by(BehaviorLog.timestamp.desc()).all()

    # 2. 提取商品ID并去重 (用户可能多次点击收藏)
    # 使用 list(dict.fromkeys()) 保持顺序去重
    product_ids = list(dict.fromkeys([log.product_id for log in logs]))

    favorites = []
    if product_ids:
        # 3. 根据ID查询商品详情，并预加载 SKU
        products = Product.query.filter(
            Product.product_id.in_(product_ids),
            Product.is_on_sale == True
        ).options(joinedload(Product.skus)).all()

        # 建立 ID -> Product 对象的映射，以便按收藏顺序排序
        product_map = {p.product_id: p for p in products}

        for pid in product_ids:
            if pid in product_map:
                favorites.append(product_map[pid])

    # 🔥 新增: 获取推荐商品
    recommendations = get_formatted_recommendations(user_id)

    return render_template('favorites.html', favorites=favorites,
                           recommendations=recommendations)  # <-- 传递 recommendations


@app.route('/admin/dashboard')
def admin_dashboard():
    if 'user_id' not in session: return redirect(url_for('login'))
    user = db.session.get(User, session['user_id'])
    if user.role != 2:
        flash('🚫 权限不足')
        return redirect(url_for('index'))

    pending_farmers = User.query.filter_by(role=1, status=2).all()
    return render_template('admin_dashboard.html', farmers=pending_farmers)


@app.route('/admin/approve/<int:user_id>')
def approve_farmer(user_id):
    if 'user_id' not in session: return redirect(url_for('login'))
    current_user = db.session.get(User, session['user_id'])
    if current_user.role != 2: return "无权操作", 403

    farmer = db.session.get(User, user_id)
    if farmer:
        farmer.status = 1
        db.session.commit()
        flash(f'✅ {farmer.username} 已审核通过！')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/train_model')
def train_model():
    """手动触发推荐算法的离线计算 (计算物品相似度)"""
    if 'user_id' not in session: return redirect(url_for('login'))
    user = db.session.get(User, session['user_id'])
    if not user or user.role != 2:
        return "无权操作", 403

    try:
        # 必须在 app context 中调用
        with app.app_context():
            recommender.calculate_and_save_similarity()
        flash('✅ 推荐模型训练完成！物品相似度矩阵已更新。')
    except Exception as e:
        print(f"训练失败: {e}")
        flash(f'❌ 模型训练失败: {e}')

    return redirect(url_for('admin_dashboard'))


@app.route('/api/collect_behavior', methods=['POST'])
def collect_behavior():
    if 'user_id' not in session:
        return jsonify({'status': 'error', 'message': '未登录'}), 401

    data = request.get_json()
    product_id = data.get('product_id')
    behavior_type = int(data.get('behavior_type'))

    if not product_id:
        return jsonify({'status': 'error', 'message': '参数错误'}), 400

    try:
        if behavior_type == 2:
            # 1. 查找所有该用户对该商品的收藏记录 (可能有多条)
            existing_logs = BehaviorLog.query.filter_by(
                user_id=session['user_id'],
                product_id=product_id,
                behavior_type=2
            ).all()

            if existing_logs:
                for log in existing_logs:
                    db.session.delete(log)
                action = 'removed'
                msg = '已取消收藏'
            else:
                new_log = BehaviorLog(
                    user_id=session['user_id'],
                    product_id=product_id,
                    behavior_type=2
                )
                db.session.add(new_log)
                action = 'added'
                msg = '收藏成功'
        else:
            new_log = BehaviorLog(
                user_id=session['user_id'],
                product_id=product_id,
                behavior_type=behavior_type
            )
            db.session.add(new_log)
            action = 'added'
            msg = '操作成功'

        db.session.commit()
        return jsonify({'status': 'success', 'action': action, 'message': msg})

    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/farmer/dashboard')
def farmer_dashboard():
    """助农数据看板：核心业务统计"""
    if 'user_id' not in session: return redirect(url_for('login'))
    user = db.session.get(User, session['user_id'])

    if not user or user.role != 1:
        flash('🚫 您不是农户，无法查看数据看板。')
        return redirect(url_for('profile'))

    # --- 1. 核心指标统计 ---
    # 累计销售额：只计算已完成 (status=4) 或售后完成 (status=7) 的订单项
    sales_stats = db.session.query(
        func.sum(OrderItem.quantity).label('total_sales'),
        func.sum(OrderItem.price * OrderItem.quantity).label('total_revenue')
    ).join(Order, OrderItem.order_id == Order.order_id).filter(
        OrderItem.farmer_id == user.user_id,
        Order.status.in_([4, 7])
    ).first()

    total_sales = sales_stats.total_sales or 0
    total_revenue = sales_stats.total_revenue or 0

    # 统计该农户所有商品的总浏览量 (PV)
    # 关联 BehaviorLog 和 Product 表
    views_stats = db.session.query(func.count(BehaviorLog.log_id)) \
        .join(Product, BehaviorLog.product_id == Product.product_id) \
        .filter(Product.farmer_id == user.user_id, BehaviorLog.behavior_type == 1) \
        .scalar()

    total_views = views_stats or 0

    # 计算转化率 (下单数 / 浏览数)
    # 注意：简单起见，这里用总销量/总浏览量估算
    conversion_rate = round((Decimal(str(total_sales)) / Decimal(str(total_views)) * Decimal('100.0')),
                            2) if total_views > 0 else Decimal('0.00')

    # --- 2. 推荐效果统计 (体现算法价值) ---
    # 统计该农户商品被“收藏”和“加购”的次数 (高意向行为)
    high_intent_stats = db.session.query(func.count(BehaviorLog.log_id)) \
        .join(Product, BehaviorLog.product_id == Product.product_id) \
        .filter(
        Product.farmer_id == user.user_id,
        BehaviorLog.behavior_type.in_([2, 3])
    ).scalar()

    # --- 3. 热销商品 Top 5 ---
    top_products = db.session.query(
        Product.name,
        func.sum(OrderItem.quantity).label('sold_count')
    ).join(OrderItem, Product.product_id == OrderItem.product_id) \
        .filter(Product.farmer_id == user.user_id) \
        .group_by(Product.product_id) \
        .order_by(func.sum(OrderItem.quantity).desc()) \
        .limit(5).all()

    return render_template('farmer_dashboard.html',
                           total_sales=total_sales,
                           total_revenue=total_revenue,
                           total_views=total_views,
                           conversion_rate=conversion_rate,
                           high_intent_count=high_intent_stats or 0,
                           top_products=top_products)


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        role = int(request.form.get('role'))

        if User.query.filter_by(username=username).first():
            flash('⚠️ 用户名已存在')
            return redirect(url_for('register'))

        new_user = User(
            username=username,
            password_hash=generate_password_hash(request.form.get('password')),
            role=role,
            status=1 if role == 0 else 2
        )
        db.session.add(new_user)
        # 如果是农户，创建 FarmerInfo
        if role == 1:
            db.session.flush()  # 确保 new_user.user_id 被设置
            farmer_info = FarmerInfo(farmer_id=new_user.user_id)
            db.session.add(farmer_info)

        db.session.commit()
        flash('注册成功，请登录')
        return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form.get('username')).first()
        if user and check_password_hash(user.password_hash, request.form.get('password')):
            session['user_id'] = user.user_id
            flash(f'👋 欢迎 {user.username}')
            return redirect(url_for('profile'))
        else:
            flash('❌ 用户名或密码错误')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


# ==========================================
# ----------------- 聊天功能 (Chat Feature) -----------------
# ==========================================

@app.route('/chat')
def chat_list():
    """聊天列表页面：显示所有与当前用户相关的会话"""
    if 'user_id' not in session:
        flash('请先登录以查看消息。')
        return redirect(url_for('login'))

    user_id = session['user_id']

    # 查找所有包含当前用户的会话，并按最后消息时间排序
    conversations = Conversation.query.filter(
        Conversation.participants.like(f"%{user_id}%")
    ).order_by(Conversation.last_message_date.desc()).all()

    chat_previews = []
    for conv in conversations:
        # 1. 识别对话的另一方
        participants_ids = [int(p) for p in conv.participants.split(',') if int(p) != user_id]
        if not participants_ids: continue

        other_user = db.session.get(User, participants_ids[0])

        # 2. 获取最后一条消息
        last_message = Message.query.filter_by(conversation_id=conv.id).order_by(Message.timestamp.desc()).first()

        # 3. 计算未读消息数
        unread_count = Message.query.filter_by(
            conversation_id=conv.id,
            status=0
        ).filter(
            Message.sender_id != user_id
        ).count()

        chat_previews.append({
            'conv_id': conv.id,
            'other_user': other_user,
            'last_message': last_message.content if last_message else "暂无消息",
            'last_message_time': last_message.timestamp if last_message else conv.last_message_date,
            'unread_count': unread_count
        })

    return render_template('chat_list.html', previews=chat_previews)


@app.route('/chat/<int:conv_id>')
def chat_detail(conv_id):
    """具体聊天窗口页面：显示历史消息"""
    if 'user_id' not in session:
        flash('请先登录。')
        return redirect(url_for('login'))

    user_id = session['user_id']
    current_user = db.session.get(User, user_id)
    conversation = Conversation.query.get_or_404(conv_id)

    # 权限检查：确保当前用户是会话参与者
    if str(user_id) not in conversation.participants.split(','):
        flash('🚫 权限不足，无法查看此会话。', 'error')
        return redirect(url_for('chat_list'))

    # 标记接收到的消息为已读
    Message.query.filter(
        Message.conversation_id == conv_id,
        Message.status == 0,
        Message.sender_id != user_id
    ).update({Message.status: 1}, synchronize_session=False)  # 增加 synchronize_session=False
    db.session.commit()

    # 获取所有消息
    messages = Message.query.filter_by(conversation_id=conv_id).order_by(Message.timestamp.asc()).all()

    # 找出对话的另一方 (即商家 ID)
    participants_ids = [int(p) for p in conversation.participants.split(',') if int(p) != user_id]
    other_user = db.session.get(User, participants_ids[0])

    # === 历史订单查询逻辑 (新增) ===
    # 1. 确定商家 ID
    merchant_id = other_user.user_id

    # 2. 查询该客户在该商家处购买的所有历史订单
    # 预加载 items, sku, product
    historical_orders = Order.query.join(OrderItem, Order.order_id == OrderItem.order_id).filter(
        Order.user_id == user_id,
        OrderItem.farmer_id == merchant_id
    ).distinct().options(
        joinedload(Order.items).joinedload(OrderItem.sku).joinedload(ProductSKU.product)
    ).order_by(Order.order_date.desc()).all()

    # 注意: chat_detail.html 模板使用的变量名是 history_orders，这里保持一致
    return render_template('chat_detail.html',
                           conversation=conversation,
                           messages=messages,
                           other_user=other_user,
                           current_user=current_user,
                           history_orders=historical_orders)  # <-- 传递历史订单


@app.route('/start_chat/<int:target_user_id>', methods=['POST'])
def start_chat(target_user_id):
    """从其他页面（如商品详情）跳转到聊天，并创建会话"""
    if 'user_id' not in session:
        flash('请先登录才能发起聊天。')
        return redirect(url_for('login'))

    current_user_id = session['user_id']

    if current_user_id == target_user_id:
        flash('不能与自己发起会话。', 'warning')
        return redirect(url_for('profile'))

    # 查找目标用户是否存在
    if not db.session.get(User, target_user_id):
        flash('目标用户不存在。', 'error')
        return redirect(url_for('index'))

    # 规范化参与者字符串：保证 ID 小的在前
    id1, id2 = sorted([current_user_id, target_user_id])
    participants_str = f"{id1},{id2}"

    conversation = Conversation.query.filter_by(participants=participants_str).first()

    if not conversation:
        # 创建新会话
        try:
            conversation = Conversation(participants=participants_str)
            db.session.add(conversation)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            flash(f'创建会话失败: {e}', 'error')
            return redirect(url_for('index'))

    return redirect(url_for('chat_detail', conv_id=conversation.id))


# ==========================================
# ⚡️ SocketIO 事件处理 (实时通信)
# ==========================================

@socketio.on('join')
def on_join(data):
    """用户连接时，加入对应的会话房间（Room）"""
    conv_id = data.get('conv_id')
    user_id = session.get('user_id')

    if conv_id and user_id:
        # 房间名称使用 conv_id
        room = str(conv_id)
        join_room(room)
        print(f"User {user_id} joined room {room}")
        # 可选：向自己发送连接成功的状态
        emit('status', {'msg': f'已连接到会话 {conv_id}'}, room=request.sid)


@socketio.on('send_message')
def handle_send_message(data):
    """处理新消息的存储和广播"""
    conv_id_raw = data.get('conv_id')
    content = data.get('content')
    sender_id = session.get('user_id')

    if not all([conv_id_raw, content, sender_id]):
        return

    conv_id = int(conv_id_raw)

    with app.app_context():
        try:
            # 1. 消息存储到数据库 (默认 status=0 未读)
            new_message = Message(
                conversation_id=conv_id,
                sender_id=sender_id,
                content=content
            )
            db.session.add(new_message)

            # 2. 更新会话的最后消息时间
            conversation = db.session.get(Conversation, conv_id)
            if conversation:
                conversation.last_message_date = datetime.now()

            db.session.commit()

            # 3. 获取发送者名称用于广播
            sender = db.session.get(User, sender_id)

            # 4. 广播消息到房间内所有连接的用户
            room = str(conv_id)
            emit('new_message', {
                'sender_id': sender_id,
                'username': sender.username,
                'content': content,
                'timestamp': new_message.timestamp.strftime('%H:%M'),
                'conv_id': conv_id
            }, room=room)

        except Exception as e:
            print(f"\n=============================================")
            print(f"❌ 聊天消息处理失败！请检查数据库日志:")
            print(f"  错误类型: {type(e).__name__}")
            print(f"  错误信息: {e}")
            print(f"=============================================\n")
            db.session.rollback()


if __name__ == '__main__':
    socketio.run(app, debug=True, port=5000)