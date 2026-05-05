import random
from datetime import datetime, timedelta
# 🔥 引入 tqdm 库
from tqdm import tqdm
from app import app, db, recommender
from models import User, Product, BehaviorLog
from werkzeug.security import generate_password_hash
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func

# --- 配置 ---
NUM_USERS = 1000  # 目标用户数量
LOGS_PER_USER = 1000  # 每个用户生成的日志数量
BATCH_SIZE = 50000  # 每次提交的日志条数 (用于优化数据库性能)

# 行为类型权重: 1:点击, 2:收藏, 3:加购, 4:购买
BEHAVIOR_CHOICES = [1] * 60 + [2] * 20 + [3] * 15 + [4] * 5


def generate_massive_data_and_train():
    with app.app_context():
        print("\n--- 启动大规模用户及行为数据生成 ---")

        # 1. 获取所有产品ID
        product_ids = [p.product_id for p in Product.query.with_entities(Product.product_id).all()]
        if not product_ids:
            print("❌ 数据库中没有在售商品，请先运行 init_products.py。")
            return
        print(f"✅ 找到 {len(product_ids)} 个商品用于生成行为日志。")

        # 2. 创建大规模用户 (test_1 to test_1000)
        print(f"--- 正在准备创建 {NUM_USERS} 个消费者用户 ---")
        users_to_create = []
        user_ids = []

        next_user_id = db.session.query(func.max(User.user_id)).scalar() or 0
        current_id = next_user_id + 1

        # 🔥 使用 tqdm 跟踪用户创建进度
        for i in tqdm(range(1, NUM_USERS + 1), desc="创建用户"):
            username = f'test_{i}'

            if db.session.query(User).filter_by(username=username).first():
                existing_user_id = db.session.query(User.user_id).filter_by(username=username).scalar()
                user_ids.append(existing_user_id)
                continue

            user_ids.append(current_id)
            users_to_create.append({
                'user_id': current_id,
                'username': username,
                'password_hash': generate_password_hash('123456'),
                'role': 0,
                'status': 1
            })
            current_id += 1

        if users_to_create:
            db.session.bulk_insert_mappings(User, users_to_create)
            db.session.commit()
            print(f"✅ 成功创建 {len(users_to_create)} 个新用户。")
        else:
            print("⚠️ 所有用户可能已存在，跳过用户创建。")

        # 3. 生成并批量插入行为日志
        all_test_user_ids = db.session.query(User.user_id).filter(User.username.like('test_%')).all()
        all_test_user_ids = [uid[0] for uid in all_test_user_ids]

        if not all_test_user_ids:
            print("❌ 无法找到任何 test_* 用户，请检查用户创建过程。")
            return

        total_target_logs = len(all_test_user_ids) * LOGS_PER_USER
        print(f"\n--- 正在生成总计 {total_target_logs} 条行为日志 ---")

        all_logs = []
        start_date = datetime.now() - timedelta(days=90)
        total_logs_count = 0

        print("⚠️ 清空这些 test_* 用户的旧行为日志...")
        BehaviorLog.query.filter(BehaviorLog.user_id.in_(all_test_user_ids)).delete(synchronize_session=False)
        db.session.commit()

        # 🔥 使用 tqdm 跟踪日志生成和插入的进度
        with tqdm(total=total_target_logs, desc="日志生成和插入", unit=" logs") as pbar:
            for user_id in all_test_user_ids:
                for _ in range(LOGS_PER_USER):

                    pid = random.choice(product_ids)
                    btype = random.choice(BEHAVIOR_CHOICES)

                    time_delta = timedelta(seconds=random.randint(0, 90 * 24 * 3600))
                    timestamp = start_date + time_delta

                    all_logs.append({
                        'user_id': user_id,
                        'product_id': pid,
                        'behavior_type': btype,
                        'timestamp': timestamp
                    })
                    total_logs_count += 1
                    pbar.update(1)  # 更新进度条

                    # 达到批次大小，进行批量插入和提交
                    if len(all_logs) >= BATCH_SIZE:
                        db.session.bulk_insert_mappings(BehaviorLog, all_logs)
                        db.session.commit()
                        # 使用 pbar.write 打印信息，避免干扰进度条
                        pbar.write(f"  -> 已提交 {total_logs_count} 条日志...")
                        all_logs = []

        # 提交剩余的日志
        if all_logs:
            db.session.bulk_insert_mappings(BehaviorLog, all_logs)
            db.session.commit()
            print(f"  -> 已提交剩余 {len(all_logs)} 条日志。")

        print(f"✅ 成功生成并插入总计 {total_logs_count} 条行为日志。")

        # 4. 触发模型训练
        print("\n--- 正在触发推荐模型离线训练 (必须!) ---")
        try:
            recommender.calculate_and_save_similarity()
            print("✅ 模型训练和大规模数据生成完成！")
        except Exception as e:
            print(f"❌ 模型训练失败: {e}")


if __name__ == '__main__':
    with app.app_context():
        db.create_all()

    generate_massive_data_and_train()