import os
import re
from decimal import Decimal

from sqlalchemy import or_

# 核心导入：确保 Product, Order, SKU, db 都已正确导入
from models import Product, Order, SKU, db
from services.assistant.intent_router import detect_intent
from services.assistant.policy_engine import check_after_sales_eligibility
from services.kg.kg_query import find_product_facts


def _build_product_cards(products, base_url):
    """构建前端展示用的商品信息卡片"""
    cards = []
    for p in products:
        # 安全计算 SKU 的最低价格
        min_price = min((sku.price for sku in p.skus), default=Decimal('0.00'))
        cards.append({
            'product_id': p.product_id,
            'name': p.name,
            'category': p.category,
            'origin': p.origin or "精选产地",
            'price': float(min_price),
            'url': f"{base_url}/product/{p.product_id}",
        })
    return cards


def _search_products(keywords, limit=5):
    """
    【全维度穿透搜索】：支持搜索商品名、分类、产地(地域)以及细分规格
    """
    if not keywords:
        return []

    query = Product.query.filter(Product.is_on_sale == True)

    cond = []
    for kw in keywords[:10]:
        kw = kw.strip()
        if not kw:
            continue
        cond.extend([
            Product.name.contains(kw),
            Product.category.contains(kw),
            # 🔥 强化点：支持按产地/地区搜索，解决“河北的产品”等查询
            Product.origin.contains(kw),
            # 穿透到 SKU 去搜，确保细分规格能被找到
            Product.skus.any(SKU.spec_name.contains(kw))
        ])

    if cond:
        result = query.filter(or_(*cond)).limit(limit).all()
        if result:
            return result

    return []


def _extract_order_id(question):
    """强化正则：支持多种订单号抓取"""
    m = re.search(r'(?:订单号?|order)\s*[:：]?\s*#?\s*(\d{1,10})', question or '', re.IGNORECASE)
    if m:
        return int(m.group(1))

    m = re.search(r'#\s*(\d{1,10})', question or '')
    if m:
        return int(m.group(1))

    return None


def _clean_llm_output(text):
    """
    【防御核心】：物理截断幻觉内容，移除 AI 常用套话
    """
    if not text:
        return ""
    # 1. 移除标签
    text = re.sub(r'^(Assistant:|客服:|机器人:|输入：|用户问题:)\s*', '', text, flags=re.IGNORECASE).strip()

    # 2. 截断模型自动续写的幻觉对话及官话
    stop_words = ["用户：", "我：", "User:", "Customer:", "Q:", "A:", "好的，谢谢", "再见", "祝您购物愉快"]
    for word in stop_words:
        if word in text:
            text = text.split(word)[0].strip()
    return text


def _handle_order_or_after_sales(question, user_id, qwen_client):
    """处理订单与售后逻辑"""
    q = question or ''
    order_id = _extract_order_id(q)

    is_after_sales = any(k in q for k in ['售后', '退款', '退货', '换货', '维权'])
    is_order_query = any(k in q for k in ['订单', '物流', '发货', '查一下']) or '【订单咨询】' in q

    if not (order_id or is_after_sales or is_order_query):
        return None

    if not user_id:
        return {
            'intent': 'after_sales',
            'answer': '亲，处理订单业务需要验证您的身份，请您先登录账号哦~',
            'recommendations': []
        }

    if not order_id:
        return {
            'intent': 'order_query',
            'answer': '帮您处理！请问您要咨询哪一单呢？\n💡 您可以点击上方 **【📋 历史订单】** 按钮直接发送给我。',
            'recommendations': []
        }

    order = Order.query.filter_by(order_id=order_id, user_id=user_id).first()
    if not order:
        return {
            'intent': 'order_query',
            'answer': f'没查到订单 **#{order_id}**，请确认下单账号或单号是否正确哦。',
            'recommendations': []
        }

    status_map = {1: '待付款', 2: '已付款待发货', 3: '已发货', 4: '已签收', 5: '已完成', 6: '售后处理中', 7: '已关闭'}
    status_text = status_map.get(order.status, f'未知状态')

    if order.status == 6:
        return {
            'intent': 'after_sales_apply',
            'answer': f'查到啦，订单 **#{order.order_id}** 正在 **【售后处理中】**。\n👉 <a href="/order/{order.order_id}" style="color: blue;">查看处理进度</a>',
            'recommendations': []
        }

    if is_after_sales:
        if order.status in [2, 3, 4]:
            order.status = 6
            reason = re.sub(r'【订单咨询】.*?\)\s*[，,]\s*购买商品[:：].*?(?=\s|$)', '', q).strip()
            order.after_sales_reason = reason[:500] if reason else "用户自主申请售后"
            db.session.commit()
            return {
                'intent': 'after_sales_apply',
                'answer': f'没问题！订单 **#{order.order_id}** 售后申请已提交。\n✅ 状态：**【售后处理中】**。\n👉 <a href="/order/{order.order_id}" style="color: blue;">点击上传凭证</a>',
                'recommendations': []
            }
        return {'intent': 'after_sales_apply', 'answer': f'订单 **#{order.order_id}** 目前是 **{status_text}**，建议联系人工客服详询。',
                'recommendations': []}

    return {
        'intent': 'order_query',
        'answer': f'查到啦！订单 **#{order.order_id}** 目前是 **【{status_text}】**。\n📦 收件人：{order.receiver_name}\n👉 <a href="/order/{order.order_id}" style="color: blue;">点击前往订单页</a>',
        'recommendations': []
    }


def build_mall_answer(qwen_client, question, user_id=None):
    """
    【去AI味、强化地域导购】商城导购逻辑
    """
    order_result = _handle_order_or_after_sales(question, user_id, qwen_client)
    if order_result is not None:
        return order_result

    # 1. 意图关键词提取（增加产地联想）
    intent_prompt = f"""请提取用户提问中的商品关键词或产地/地区关键词。
只输出词语，用空格隔开，严禁任何额外解释。
用户：{question}
输出："""
    smart_keywords_str = _clean_llm_output(qwen_client.generate(intent_prompt))

    if not smart_keywords_str or len(smart_keywords_str) > 50:
        cleaned_q = re.sub(r'我想|买点|吃|要|推荐|有没有|的|呢|吧|啊', ' ', question)
        keywords = [k.strip() for k in cleaned_q.split() if k.strip()]
    else:
        keywords = [k.strip() for k in smart_keywords_str.split() if k.strip()]

    # 2. 数据库全维度搜索
    base_url = os.getenv('MALL_BASE_URL', 'http://127.0.0.1:5000')
    products = _search_products(keywords)
    product_cards = _build_product_cards(products, base_url)

    # 3. 没搜到货时的处理：热情、引导、展示商城优势
    if not product_cards:
        fallback_prompt = f"""
        用户想找：'{question}'。目前商城库存未命中。
        要求：
        1. 变身为热情的金牌导购，严禁使用“非常抱歉”、“由于条件限制”等机械话术。
        2. 提及商城优势：所有产品均为基地直采、顺丰冷链、严格品控。
        3. 建议用户看看我们其他的当季尖货。
        4. 绝对不要演剧本，不要替用户说话。
        直接输出回复。
        """
        llm_text = qwen_client.generate(fallback_prompt)
        return {
            'intent': 'mall_assistant',
            'answer': _clean_llm_output(llm_text) or '哎呀，您提到的这款宝贝暂时还没上架，但咱们商城主打基地直销，您可以逛逛我们的其他尖货！',
            'recommendations': []
        }

    # 4. 搜到货了：真人化推荐
    formatted_products = ""
    for idx, item in enumerate(product_cards[:5], 1):
        formatted_products += f"{idx}. {item['name']} (产地:{item['origin']}), 价格: ¥{item['price']}, 链接: {item['url']}\n"

    # 🔥 强化 Prompt：融入产地特色，强化信任感
    prompt = f"""
    你是商城资深导购。请根据【候选商品】回复【用户提问】。
    要求：
    1. 说话要干脆热情，自然嵌入商品产地和链接。
    2. 严禁使用“为您推荐以下商品”、“Assistant:”等废话。
    3. 强调咱们商城的商品都是精挑细选的，品质有保障。
    4. 禁止演剧本，不要生成用户反馈。

    【候选商品】
    {formatted_products}

    【用户提问】
    {question}
    """
    llm_text = qwen_client.generate(prompt)
    final_answer = _clean_llm_output(llm_text)

    return {
        'intent': 'mall_assistant',
        'answer': final_answer or '这就为您找来了几款优质好物，您看看有没有中意的！',
        'recommendations': product_cards[:5]
    }


def build_answer(graph_client, qwen_client, merchant_id, user_id, question, order_id=None):
    """强化后的商家详情客服逻辑：深度联动知识图谱事实"""
    intent = detect_intent(question)
    # 🔥 强化点：查知识图谱事实，涵盖地域、品质等
    facts = find_product_facts(graph_client, merchant_id, question[:15])
    policy = check_after_sales_eligibility(user_id, order_id) if intent == 'after_sales' and order_id else None

    prompt = f"""
    你是商家客服专家。请根据【事实数据】专业、亲切地回复用户。
    要求：
    1. 依据事实回答，严禁编造。
    2. 严禁输出后续对话剧本。

    商家ID: {merchant_id} | 问题: {question} | 事实数据: {facts} | 售后判定: {policy}
    直接输出回复。
    """
    llm_text = qwen_client.generate(prompt)
    answer = _clean_llm_output(llm_text)

    return {
        'intent': intent,
        'answer': answer,
        'facts': facts,
        'policy_result': policy,
    }