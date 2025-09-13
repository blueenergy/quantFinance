import os
import subprocess
import datetime
import json
from pymongo import MongoClient
from dotenv import load_dotenv
import argparse
import logging

load_dotenv()

class MongoMigrator:
    """MongoDB 集合自动化迁移工具"""
    
    def __init__(self, config_file=None):
        """
        初始化迁移工具
        
        Args:
            config_file: 配置文件路径
        """
        self.setup_logging()
        
        if config_file:
            self.load_config(config_file)
        else:
            self.load_env_config()
    
    def setup_logging(self):
        """设置日志"""
        log_format = '%(asctime)s - %(levelname)s - %(message)s'
        logging.basicConfig(
            level=logging.INFO,
            format=log_format,
            handlers=[
                logging.FileHandler(f'mongo_migration_{datetime.datetime.now().strftime("%Y%m%d")}.log'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)
    
    def load_env_config(self):
        """从环境变量加载配置"""
        self.source_config = {
            'host': os.getenv('SOURCE_MONGO_HOST', 'localhost'),
            'port': int(os.getenv('SOURCE_MONGO_PORT', 27017)),
            'database': os.getenv('SOURCE_MONGO_DB', 'finance'),
            'username': os.getenv('SOURCE_MONGO_USER'),
            'password': os.getenv('SOURCE_MONGO_PASS'),
            'auth_db': os.getenv('SOURCE_MONGO_AUTH_DB', 'admin')
        }
        
        self.target_config = {
            'host': os.getenv('TARGET_MONGO_HOST', 'localhost'),
            'port': int(os.getenv('TARGET_MONGO_PORT', 27017)),
            'database': os.getenv('TARGET_MONGO_DB', 'finance'),
            'username': os.getenv('TARGET_MONGO_USER'),
            'password': os.getenv('TARGET_MONGO_PASS'),
            'auth_db': os.getenv('TARGET_MONGO_AUTH_DB', 'admin')
        }
        
        # 备份目录
        self.backup_dir = os.getenv('BACKUP_DIR', './mongodb_backups')
        
        self.logger.info("✅ 从环境变量加载配置完成")
    
    def load_config(self, config_file):
        """从配置文件加载配置"""
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            
            self.source_config = config['source']
            self.target_config = config['target']
            self.backup_dir = config.get('backup_dir', './mongodb_backups')
            
            self.logger.info(f"✅ 从配置文件 {config_file} 加载配置完成")
            
        except Exception as e:
            self.logger.error(f"❌ 加载配置文件失败: {e}")
            raise
    
    def build_mongo_uri(self, config):
        """构建 MongoDB 连接 URI"""
        if config['username'] and config['password']:
            auth_part = f"{config['username']}:{config['password']}@"
            auth_options = f"?authSource={config['auth_db']}"
        else:
            auth_part = ""
            auth_options = ""
        
        uri = f"mongodb://{auth_part}{config['host']}:{config['port']}/{config['database']}{auth_options}"
        return uri
    
    def export_collection_mongodump(self, collection_name, query=None):
        """
        🔧 使用 mongodump 导出集合
        
        Args:
            collection_name: 集合名称
            query: 查询条件 (JSON 字符串)
            
        Returns:
            str: 导出文件路径
        """
        try:
            # 创建备份目录
            timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            export_dir = os.path.join(self.backup_dir, f"{collection_name}_{timestamp}")
            os.makedirs(export_dir, exist_ok=True)
            
            # 构建 mongodump 命令
            cmd = [
                'mongodump',
                '--host', f"{self.source_config['host']}:{self.source_config['port']}",
                '--db', self.source_config['database'],
                '--collection', collection_name,
                '--out', export_dir
            ]
            
            # 添加认证信息
            if self.source_config['username']:
                cmd.extend(['--username', self.source_config['username']])
                cmd.extend(['--password', self.source_config['password']])
                cmd.extend(['--authenticationDatabase', self.source_config['auth_db']])
            
            # 添加查询条件
            if query:
                cmd.extend(['--query', query])
            
            self.logger.info(f"🚀 开始导出集合 {collection_name}...")
            self.logger.info(f"📁 导出目录: {export_dir}")
            
            # 执行命令
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            
            self.logger.info(f"✅ 集合 {collection_name} 导出成功")
            self.logger.info(f"📊 导出信息: {result.stdout}")
            
            return export_dir
            
        except subprocess.CalledProcessError as e:
            self.logger.error(f"❌ mongodump 执行失败: {e}")
            self.logger.error(f"错误输出: {e.stderr}")
            raise
        except Exception as e:
            self.logger.error(f"❌ 导出集合失败: {e}")
            raise
    
    def import_collection_mongorestore(self, dump_dir, collection_name, drop_existing=True):
        """
        🔧 使用 mongorestore 导入集合
        
        Args:
            dump_dir: dump 文件目录
            collection_name: 集合名称
            drop_existing: 是否删除现有集合
            
        Returns:
            bool: 导入是否成功
        """
        try:
            # 构建 mongorestore 命令
            bson_file = os.path.join(dump_dir, self.source_config['database'], f"{collection_name}.bson")
            
            if not os.path.exists(bson_file):
                self.logger.error(f"❌ 找不到 BSON 文件: {bson_file}")
                return False
            
            cmd = [
                'mongorestore',
                '--host', f"{self.target_config['host']}:{self.target_config['port']}",
                '--db', self.target_config['database'],
                '--collection', collection_name
            ]
            
            # 添加认证信息
            if self.target_config['username']:
                cmd.extend(['--username', self.target_config['username']])
                cmd.extend(['--password', self.target_config['password']])
                cmd.extend(['--authenticationDatabase', self.target_config['auth_db']])
            
            # 是否删除现有集合
            if drop_existing:
                cmd.append('--drop')
            
            # 添加 BSON 文件路径
            cmd.append(bson_file)
            
            self.logger.info(f"🚀 开始导入集合 {collection_name}...")
            self.logger.info(f"📁 BSON 文件: {bson_file}")
            
            # 执行命令
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            
            self.logger.info(f"✅ 集合 {collection_name} 导入成功")
            self.logger.info(f"📊 导入信息: {result.stdout}")
            
            return True
            
        except subprocess.CalledProcessError as e:
            self.logger.error(f"❌ mongorestore 执行失败: {e}")
            self.logger.error(f"错误输出: {e.stderr}")
            return False
        except Exception as e:
            self.logger.error(f"❌ 导入集合失败: {e}")
            return False
    
    def migrate_collection(self, collection_name, query=None, drop_existing=True):
        """
        🔧 完整的集合迁移流程
        
        Args:
            collection_name: 集合名称
            query: 导出查询条件
            drop_existing: 是否删除目标集合
            
        Returns:
            bool: 迁移是否成功
        """
        try:
            self.logger.info(f"🎯 开始迁移集合: {collection_name}")
            
            # 1. 导出集合
            dump_dir = self.export_collection_mongodump(collection_name, query)
            
            # 2. 导入集合
            success = self.import_collection_mongorestore(dump_dir, collection_name, drop_existing)
            
            if success:
                self.logger.info(f"🎉 集合 {collection_name} 迁移完成")
                
                # 验证迁移结果
                self.verify_migration(collection_name)
                
                return True
            else:
                self.logger.error(f"❌ 集合 {collection_name} 迁移失败")
                return False
                
        except Exception as e:
            self.logger.error(f"❌ 迁移过程出错: {e}")
            return False
    
    def verify_migration(self, collection_name):
        """验证迁移结果"""
        try:
            # 连接源数据库
            source_uri = self.build_mongo_uri(self.source_config)
            source_client = MongoClient(source_uri)
            source_db = source_client[self.source_config['database']]
            source_count = source_db[collection_name].count_documents({})
            
            # 连接目标数据库
            target_uri = self.build_mongo_uri(self.target_config)
            target_client = MongoClient(target_uri)
            target_db = target_client[self.target_config['database']]
            target_count = target_db[collection_name].count_documents({})
            
            self.logger.info(f"📊 迁移验证:")
            self.logger.info(f"   源集合文档数: {source_count}")
            self.logger.info(f"   目标集合文档数: {target_count}")
            
            if source_count == target_count:
                self.logger.info("✅ 迁移验证通过")
            else:
                self.logger.warning("⚠️ 文档数量不匹配，请检查")
            
            # 关闭连接
            source_client.close()
            target_client.close()
            
        except Exception as e:
            self.logger.error(f"❌ 验证迁移失败: {e}")
    
    def migrate_multiple_collections(self, collections, query_dict=None):
        """
        🔧 批量迁移多个集合
        
        Args:
            collections: 集合名称列表
            query_dict: 每个集合的查询条件字典
            
        Returns:
            dict: 迁移结果统计
        """
        results = {
            'successful': [],
            'failed': [],
            'total': len(collections)
        }
        
        self.logger.info(f"🎯 开始批量迁移 {len(collections)} 个集合")
        
        for collection in collections:
            query = query_dict.get(collection) if query_dict else None
            
            try:
                success = self.migrate_collection(collection, query)
                
                if success:
                    results['successful'].append(collection)
                else:
                    results['failed'].append(collection)
                    
            except Exception as e:
                self.logger.error(f"❌ 迁移 {collection} 失败: {e}")
                results['failed'].append(collection)
        
        # 打印结果统计
        self.logger.info(f"\n📊 批量迁移结果:")
        self.logger.info(f"   总集合数: {results['total']}")
        self.logger.info(f"   成功: {len(results['successful'])}")
        self.logger.info(f"   失败: {len(results['failed'])}")
        
        if results['failed']:
            self.logger.warning(f"   失败的集合: {results['failed']}")
        
        return results


# 🔧 专门针对您的 finance 数据库的迁移工具
class FinanceDbMigrator(MongoMigrator):
    """专门用于 finance 数据库的迁移工具"""
    
    def __init__(self, config_file=None):
        super().__init__(config_file)
        
        # 定义 finance 数据库的主要集合
        self.finance_collections = [
            'stock_info',           # 股票基本信息
            'stock_scores',         # 股票评分
            'volume_price',         # 量价数据
            'money_flow_records',   # 资金流向记录
            'financial_data',       # 财务数据
            'technical_indicators', # 技术指标
            'market_data'          # 市场数据
        ]
    
    def migrate_finance_db(self, specific_collections=None, date_filter=None):
        """
        🔧 迁移整个 finance 数据库
        
        Args:
            specific_collections: 指定迁移的集合列表，如果为 None 则迁移所有
            date_filter: 日期过滤条件
            
        Returns:
            dict: 迁移结果
        """
        collections_to_migrate = specific_collections or self.finance_collections
        
        # 构建查询条件
        query_dict = {}
        if date_filter:
            for collection in collections_to_migrate:
                if collection == 'stock_scores':
                    query_dict[collection] = f'{{"score_date": "{date_filter}"}}'
                elif collection in ['volume_price', 'money_flow_records']:
                    query_dict[collection] = f'{{"date": "{date_filter}"}}'
        
        return self.migrate_multiple_collections(collections_to_migrate, query_dict)
    
    def migrate_latest_scores(self, days=7):
        """
        🔧 迁移最近几天的评分数据
        
        Args:
            days: 迁移最近几天的数据
        """
        try:
            # 计算日期范围
            end_date = datetime.datetime.now()
            start_date = end_date - datetime.timedelta(days=days)
            
            date_filter = {
                "$gte": start_date.strftime('%Y%m%d'),
                "$lte": end_date.strftime('%Y%m%d')
            }
            
            query = json.dumps({"score_date": date_filter})
            
            self.logger.info(f"🎯 迁移最近 {days} 天的评分数据")
            self.logger.info(f"📅 日期范围: {start_date.strftime('%Y-%m-%d')} 至 {end_date.strftime('%Y-%m-%d')}")
            
            return self.migrate_collection('stock_scores', query)
            
        except Exception as e:
            self.logger.error(f"❌ 迁移最近评分数据失败: {e}")
            return False


def create_config_file():
    """创建配置文件模板"""
    config_template = {
        "source": {
            "host": "source-mongodb-host",
            "port": 27017,
            "database": "finance",
            "username": "source_user",
            "password": "source_password",
            "auth_db": "admin"
        },
        "target": {
            "host": "target-mongodb-host",
            "port": 27017,
            "database": "finance",
            "username": "target_user",
            "password": "target_password",
            "auth_db": "admin"
        },
        "backup_dir": "./mongodb_backups"
    }
    
    with open('mongo_migration_config.json', 'w', encoding='utf-8') as f:
        json.dump(config_template, f, indent=2, ensure_ascii=False)
    
    print("✅ 配置文件模板已创建: mongo_migration_config.json")
    print("请编辑配置文件后使用")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='MongoDB 集合自动化迁移工具')
    parser.add_argument('--config', '-c', type=str, help='配置文件路径')
    parser.add_argument('--action', '-a', 
                        choices=['migrate-all', 'migrate-scores', 'migrate-latest', 'create-config'],
                        default='migrate-scores', help='执行动作')
    parser.add_argument('--collections', '-col', nargs='+', help='指定集合名称')
    parser.add_argument('--date', '-d', type=str, help='日期过滤 (YYYYMMDD)')
    parser.add_argument('--days', type=int, default=7, help='最近几天的数据')
    
    args = parser.parse_args()
    
    if args.action == 'create-config':
        create_config_file()
        return
    
    try:
        # 创建迁移器
        migrator = FinanceDbMigrator(args.config)
        
        if args.action == 'migrate-all':
            # 迁移所有集合
            result = migrator.migrate_finance_db(args.collections, args.date)
            
        elif args.action == 'migrate-scores':
            # 只迁移评分数据
            if args.date:
                query = f'{{"score_date": "{args.date}"}}'
                result = migrator.migrate_collection('stock_scores', query)
            else:
                result = migrator.migrate_collection('stock_scores')
                
        elif args.action == 'migrate-latest':
            # 迁移最近几天的评分数据
            result = migrator.migrate_latest_scores(args.days)
        
        print(f"\n🎉 迁移完成，结果: {result}")
        
    except Exception as e:
        print(f"❌ 迁移过程出错: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()