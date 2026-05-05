import os
from flask import Blueprint, request, jsonify, session

from services.kg.graph_client import GraphClient
from services.kg.kg_builder import upsert_product
from services.llm.qwen_client import QwenClient
from services.assistant.response_orchestrator import build_answer, build_mall_answer

assistant_bp = Blueprint('assistant', __name__, url_prefix='/api/assistant')

graph_client = GraphClient()
qwen_client = QwenClient()
qwen_client.warmup()


@assistant_bp.route('/chat', methods=['POST'])
def assistant_chat():
    data = request.get_json(force=True)
    merchant_id = data.get('merchant_id')
    question = data.get('question', '').strip()
    order_id = data.get('order_id')
    user_id = session.get('user_id') or data.get('user_id')

    if not merchant_id or not question or not user_id:
        return jsonify({'ok': False, 'message': 'merchant_id/question/user_id 必填'}), 400

    result = build_answer(graph_client, qwen_client, int(merchant_id), int(user_id), question, order_id)
    return jsonify({'ok': True, **result})


@assistant_bp.route('/sync_product', methods=['POST'])
def sync_product_to_kg():
    data = request.get_json(force=True)
    merchant_id = data.get('merchant_id')
    product_id = data.get('product_id')
    if not merchant_id or not product_id:
        return jsonify({'ok': False, 'message': 'merchant_id/product_id 必填'}), 400

    success = upsert_product(graph_client, int(product_id), int(merchant_id))
    return jsonify({'ok': success})




@assistant_bp.route('/mall_chat', methods=['POST'])
def mall_assistant_chat():
    data = request.get_json(force=True)
    question = (data.get('question') or '').strip()
    if not question:
        return jsonify({'ok': False, 'message': 'question 必填'}), 400

    user_id = session.get('user_id') or data.get('user_id')
    result = build_mall_answer(qwen_client, question, user_id=user_id)
    return jsonify({'ok': True, **result})

@assistant_bp.route('/health', methods=['GET'])
def assistant_health():
    return jsonify({
        'ok': True,
        'assistant_ready': True,
        'kg_enabled': graph_client.enabled,
        'kg_database': graph_client.database,
        'kg_source_tag': os.getenv('KG_SOURCE_TAG', 'mall_assistant_v2'),
        'llm_enabled': qwen_client.enabled,
        'llm_backend': qwen_client.backend,
        'llm_model_path': qwen_client.model_path,
    })
