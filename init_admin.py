# 文件名: init_admin.py
from app import app, db
from models import User
from werkzeug.security import generate_password_hash


def create_admin():
    with app.app_context():
        # 1. 检查是否已经存在名为 admin 的用户
        admin = User.query.filter_by(username='admin').first()

        if admin:
            print("⚠️ 管理员账号 'admin' 已存在，无需重复创建。")
            # 如果想重置密码，可以在这里写更新逻辑
        else:
            # 2. 创建管理员用户
            # role=2 代表管理员 (0=消费者, 1=农户)
            # status=1 代表账号状态正常
            new_admin = User(
                username='admin',
                password_hash=generate_password_hash('123456'),
                role=2,
                status=1
            )
            db.session.add(new_admin)
            db.session.commit()
            print("✅ 管理员账号创建成功！")
            print("   用户名: admin")
            print("   密  码: 123456")


if __name__ == '__main__':
    create_admin()