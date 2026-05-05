import os
from neo4j import GraphDatabase


class GraphClient:
    def __init__(self):
        self.enabled = os.getenv('NEO4J_ENABLED', '0') == '1'
        self.database = os.getenv('NEO4J_DATABASE', 'mall_assistant_kg')
        self._driver = None

        if self.enabled:
            uri = os.getenv('NEO4J_URI', 'bolt://127.0.0.1:7687')
            user = os.getenv('NEO4J_USER', 'neo4j')
            pwd = os.getenv('NEO4J_PASSWORD', '12345678')
            self._driver = GraphDatabase.driver(uri, auth=(user, pwd))
            self._ensure_database_compatible()

    def _ensure_database_compatible(self):
        """
        尝试确保目标数据库可用。
        - 企业版支持多数据库：尝试 CREATE DATABASE IF NOT EXISTS
        - 社区版不支持该命令：自动降级到默认 neo4j 数据库
        """
        if not self._driver:
            return

        try:
            with self._driver.session(database='system') as session:
                session.run(f"CREATE DATABASE `{self.database}` IF NOT EXISTS")
            print(f"✅ Neo4j 数据库已确认存在: {self.database}")
        except Exception as e:
            msg = str(e)
            if 'Unsupported administration command' in msg or 'Neo.ClientError.Statement.UnsupportedAdministrationCommand' in msg:
                self.database = os.getenv('NEO4J_FALLBACK_DATABASE', 'neo4j')
                print(f"⚠️ 当前 Neo4j 版本不支持多数据库，已自动回退到数据库: {self.database}")
            else:
                print(f"⚠️ Neo4j 数据库创建/检查失败，继续使用当前配置: {e}")

    def run(self, cypher, **params):
        if not self.enabled or self._driver is None:
            return []
        try:
            with self._driver.session(database=self.database) as session:
                result = session.run(cypher, **params)
                return [r.data() for r in result]
        except Exception as e:
            print(f"⚠️ Neo4j 连接异常，已跳过本次写入/查询: {e}")
            self.enabled = False
            return []

    def close(self):
        if self._driver:
            self._driver.close()
