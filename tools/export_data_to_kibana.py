import os
from pymongo import MongoClient
from elasticsearch import Elasticsearch
from datetime import datetime, timedelta
import json
from dotenv import load_dotenv

load_dotenv()

class StockDataExporter:
    def __init__(self):
        # MongoDB 连接
        self.mongo_client = MongoClient(os.getenv("MONGO_URL", "mongodb://localhost:27017"))
        self.mongo_db = self.mongo_client["finance"]
        
        # ✅ 修复 Elasticsearch 连接参数
        try:
            # 方法1：带认证的连接方式
            self.es = Elasticsearch(
                hosts=["http://10.183.72.185:9200"],
                basic_auth=("elastic", "changeme"),  # 替换为实际的用户名和密码
                verify_certs=False,
                ssl_show_warn=False
            )
            
            # 测试连接
            info = self.es.info()
            print(f"✅ Elasticsearch 连接成功: {info['version']['number']}")
            
        except Exception as e:
            print(f"❌ 带认证连接失败: {e}")
            try:
                # 方法2：无认证连接（如果集群配置为开放访问）
                self.es = Elasticsearch(
                    hosts=["http://10.183.72.185:9200"],
                    verify_certs=False
                )
                
                # 测试连接
                info = self.es.info()
                print(f"✅ Elasticsearch 连接成功 (无认证): {info['version']['number']}")
                
            except Exception as e2:
                print(f"❌ 无认证连接也失败: {e2}")
                try:
                    # 方法3：使用环境变量配置
                    es_host = os.getenv("ELASTICSEARCH_HOST", "http://localhost:9200")
                    es_user = os.getenv("ELASTICSEARCH_USER", "elastic")
                    es_pass = os.getenv("ELASTICSEARCH_PASSWORD", "changeme")
                    
                    self.es = Elasticsearch(
                        hosts=[es_host],
                        basic_auth=(es_user, es_pass) if es_user and es_pass else None,
                        verify_certs=False
                    )
                    
                    info = self.es.info()
                    print(f"✅ Elasticsearch 连接成功 (环境变量): {info['version']['number']}")
                    
                except Exception as e3:
                    print(f"❌ 所有连接方法都失败: {e3}")
                    print("请检查:")
                    print("1. Elasticsearch 服务是否运行")
                    print("2. IP 地址和端口是否正确")
                    print("3. 认证凭据是否正确")
                    print("4. 网络连接是否正常")
                    raise
        
        # 定义索引名称
        self.indices = {
            'stock_prices': 'stock-prices',
            'stock_scores': 'stock-scores', 
            'money_flow': 'money-flow',
            'stock_info': 'stock-info'
        }
    
    def create_index_templates(self):
        """创建 Elasticsearch 索引模板"""
        
        # 股价数据索引模板
        stock_price_template = {
            "index_patterns": ["stock-prices*"],
            "template": {
                "settings": {
                    "number_of_shards": 1,
                    "number_of_replicas": 0,
                    "index.refresh_interval": "5s"
                },
                "mappings": {
                    "properties": {
                        "symbol": {"type": "keyword"},
                        "trade_date": {"type": "date", "format": "yyyyMMdd"},
                        "open": {"type": "float"},
                        "high": {"type": "float"},
                        "low": {"type": "float"},
                        "close": {"type": "float"},
                        "volume": {"type": "long"},
                        "amount": {"type": "float"},
                        "timestamp": {"type": "date"}
                    }
                }
            }
        }
        
        # 评分数据索引模板
        stock_scores_template = {
            "index_patterns": ["stock-scores*"],
            "template": {
                "settings": {
                    "number_of_shards": 1,
                    "number_of_replicas": 0
                },
                "mappings": {
                    "properties": {
                        "symbol": {"type": "keyword"},
                        "score_date": {"type": "date", "format": "yyyyMMdd"},
                        "cycle_score": {"type": "float"},
                        "fundamental_score": {"type": "float"},
                        "technical_score": {"type": "float"},
                        "money_flow_score": {"type": "float"},
                        "total_score": {"type": "float"},
                        "timestamp": {"type": "date"}
                    }
                }
            }
        }
        
        # 资金流数据索引模板
        money_flow_template = {
            "index_patterns": ["money-flow*"],
            "template": {
                "settings": {
                    "number_of_shards": 1,
                    "number_of_replicas": 0
                },
                "mappings": {
                    "properties": {
                        "symbol": {"type": "keyword"},
                        "trade_date": {"type": "date", "format": "yyyyMMdd"},
                        "buy_sm_amount": {"type": "float"},
                        "buy_md_amount": {"type": "float"},
                        "buy_lg_amount": {"type": "float"},
                        "buy_elg_amount": {"type": "float"},
                        "sell_sm_amount": {"type": "float"},
                        "sell_md_amount": {"type": "float"},
                        "sell_lg_amount": {"type": "float"},
                        "sell_elg_amount": {"type": "float"},
                        "net_buy_amount": {"type": "float"},
                        "timestamp": {"type": "date"}
                    }
                }
            }
        }
        
        # 股票信息索引模板
        stock_info_template = {
            "index_patterns": ["stock-info*"],
            "template": {
                "settings": {
                    "number_of_shards": 1,
                    "number_of_replicas": 0
                },
                "mappings": {
                    "properties": {
                        "symbol": {"type": "keyword"},
                        "name": {
                            "type": "text",
                            "fields": {
                                "keyword": {"type": "keyword"}
                            }
                        },
                        "industry": {"type": "keyword"},
                        "area": {"type": "keyword"},
                        "market": {"type": "keyword"},
                        "list_date": {"type": "date", "format": "yyyyMMdd"},
                        "total_share": {"type": "float"},
                        "flow_share": {"type": "float"},
                        "timestamp": {"type": "date"}
                    }
                }
            }
        }
        
        # 创建索引模板
        templates = {
            'stock_prices': stock_price_template,
            'stock_scores': stock_scores_template,
            'money_flow': money_flow_template,
            'stock_info': stock_info_template
        }
        
        for template_name, template_body in templates.items():
            try:
                # ✅ 修复 API 调用方式
                response = self.es.indices.put_index_template(
                    name=template_name,
                    **template_body  # ✅ 使用解包方式传递参数
                )
                print(f"✅ 创建索引模板: {template_name}")
            except Exception as e:
                print(f"❌ 创建索引模板失败 {template_name}: {e}")
                # ✅ 尝试旧版本 API
                try:
                    # 移除新版本特有的结构
                    old_template = {
                        "index_patterns": template_body["index_patterns"],
                        "settings": template_body["template"]["settings"],
                        "mappings": template_body["template"]["mappings"]
                    }
                    self.es.indices.put_template(
                        name=template_name,
                        body=old_template
                    )
                    print(f"✅ 使用旧API创建索引模板: {template_name}")
                except Exception as e2:
                    print(f"❌ 旧API也失败了 {template_name}: {e2}")
                    # 继续处理下一个模板，不中断程序
                    continue
    
    def export_stock_prices(self, symbols=None, days=30):
        """导出股价数据到 Elasticsearch"""
        print(f"\n=== 导出股价数据 (最近{days}天) ===")
        
        # 计算查询日期范围
        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')
        
        query = {
            "trade_date": {"$gte": start_date, "$lte": end_date}
        }
        
        if symbols:
            query["symbol"] = {"$in": symbols}
        
        volume_price_collection = self.mongo_db["volume_price"]
        
        # ✅ 添加查询统计
        total_records = volume_price_collection.count_documents(query)
        print(f"📊 MongoDB 中符合条件的记录数: {total_records}")
        
        if total_records == 0:
            print("⚠️  没有找到符合条件的股价数据")
            return
        
        cursor = volume_price_collection.find(query)
        
        batch_size = 1000
        batch = []
        total_count = 0
        
        for doc in cursor:
            try:
                # 转换 MongoDB 文档为 ES 文档
                es_doc = {
                    "symbol": str(doc["symbol"]),
                    "trade_date": str(doc["trade_date"]),
                    "open": float(doc.get("open", 0)) if doc.get("open") else 0.0,
                    "high": float(doc.get("high", 0)) if doc.get("high") else 0.0,
                    "low": float(doc.get("low", 0)) if doc.get("low") else 0.0,
                    "close": float(doc.get("close", 0)) if doc.get("close") else 0.0,
                    "volume": int(doc.get("volume", 0)) if doc.get("volume") else 0,
                    "amount": float(doc.get("amount", 0)) if doc.get("amount") else 0.0,
                    "timestamp": datetime.now()
                }
                
                batch.append({
                    "_index": self.indices['stock_prices'],
                    "_source": es_doc
                })
                
                if len(batch) >= batch_size:
                    self._bulk_index(batch)
                    total_count += len(batch)
                    batch = []
                    print(f"已导入 {total_count} 条股价记录...")
                    
            except Exception as e:
                print(f"⚠️  跳过无效记录: {doc.get('symbol', 'Unknown')}, 错误: {e}")
                continue
        
        # 处理最后一批
        if batch:
            self._bulk_index(batch)
            total_count += len(batch)
        
        print(f"✅ 股价数据导入完成，共 {total_count} 条记录")
    
    def export_stock_scores(self, days=30):
        """导出评分数据到 Elasticsearch"""
        print(f"\n=== 导出评分数据 (最近{days}天) ===")
        
        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')
        
        query = {
            "score_date": {"$gte": start_date, "$lte": end_date}
        }
        
        stock_scores_collection = self.mongo_db["stock_scores"]
        
        # ✅ 添加查询统计
        total_records = stock_scores_collection.count_documents(query)
        print(f"📊 MongoDB 中符合条件的评分记录数: {total_records}")
        
        if total_records == 0:
            print("⚠️  没有找到符合条件的评分数据")
            return
        
        cursor = stock_scores_collection.find(query)
        
        batch = []
        total_count = 0
        
        for doc in cursor:
            try:
                es_doc = {
                    "symbol": str(doc["symbol"]),
                    "score_date": str(doc["score_date"]),
                    "cycle_score": float(doc.get("cycle_score", 0)) if doc.get("cycle_score") is not None else 0.0,
                    "fundamental_score": float(doc.get("fundamental_score", 0)) if doc.get("fundamental_score") is not None else 0.0,
                    "technical_score": float(doc.get("technical_score", 0)) if doc.get("technical_score") is not None else 0.0,
                    "money_flow_score": float(doc.get("money_flow_score", 0)) if doc.get("money_flow_score") is not None else 0.0,
                    "total_score": float(doc.get("total_score", 0)) if doc.get("total_score") is not None else 0.0,
                    "timestamp": datetime.now()
                }
                
                batch.append({
                    "_index": self.indices['stock_scores'],
                    "_source": es_doc
                })
                
                if len(batch) >= 1000:
                    self._bulk_index(batch)
                    total_count += len(batch)
                    batch = []
                    print(f"已导入 {total_count} 条评分记录...")
                    
            except Exception as e:
                print(f"⚠️  跳过无效评分记录: {doc.get('symbol', 'Unknown')}, 错误: {e}")
                continue
        
        if batch:
            self._bulk_index(batch)
            total_count += len(batch)
        
        print(f"✅ 评分数据导入完成，共 {total_count} 条记录")
    
    def export_money_flow(self, symbols=None, days=30):
        """导出资金流数据到 Elasticsearch"""
        print(f"\n=== 导出资金流数据 (最近{days}天) ===")
        
        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')
        
        query = {
            "trade_date": {"$gte": start_date, "$lte": end_date}
        }
        
        if symbols:
            query["symbol"] = {"$in": symbols}
        
        money_flow_collection = self.mongo_db["money_flow_records"]
        
        # ✅ 添加查询统计
        total_records = money_flow_collection.count_documents(query)
        print(f"📊 MongoDB 中符合条件的资金流记录数: {total_records}")
        
        if total_records == 0:
            print("⚠️  没有找到符合条件的资金流数据")
            return
        
        cursor = money_flow_collection.find(query)
        
        batch = []
        total_count = 0
        
        for doc in cursor:
            try:
                # 安全地获取数值并计算净买入金额
                buy_lg = float(doc.get("buy_lg_amount", 0)) if doc.get("buy_lg_amount") is not None else 0.0
                buy_elg = float(doc.get("buy_elg_amount", 0)) if doc.get("buy_elg_amount") is not None else 0.0
                sell_lg = float(doc.get("sell_lg_amount", 0)) if doc.get("sell_lg_amount") is not None else 0.0
                sell_elg = float(doc.get("sell_elg_amount", 0)) if doc.get("sell_elg_amount") is not None else 0.0
                
                net_buy = buy_lg + buy_elg - sell_lg - sell_elg
                
                es_doc = {
                    "symbol": str(doc["symbol"]),
                    "trade_date": str(doc["trade_date"]),
                    "buy_sm_amount": float(doc.get("buy_sm_amount", 0)) if doc.get("buy_sm_amount") is not None else 0.0,
                    "buy_md_amount": float(doc.get("buy_md_amount", 0)) if doc.get("buy_md_amount") is not None else 0.0,
                    "buy_lg_amount": buy_lg,
                    "buy_elg_amount": buy_elg,
                    "sell_sm_amount": float(doc.get("sell_sm_amount", 0)) if doc.get("sell_sm_amount") is not None else 0.0,
                    "sell_md_amount": float(doc.get("sell_md_amount", 0)) if doc.get("sell_md_amount") is not None else 0.0,
                    "sell_lg_amount": sell_lg,
                    "sell_elg_amount": sell_elg,
                    "net_buy_amount": net_buy,
                    "timestamp": datetime.now()
                }
                
                batch.append({
                    "_index": self.indices['money_flow'],
                    "_source": es_doc
                })
                
                if len(batch) >= 1000:
                    self._bulk_index(batch)
                    total_count += len(batch)
                    batch = []
                    print(f"已导入 {total_count} 条资金流记录...")
                    
            except Exception as e:
                print(f"⚠️  跳过无效资金流记录: {doc.get('symbol', 'Unknown')}, 错误: {e}")
                continue
        
        if batch:
            self._bulk_index(batch)
            total_count += len(batch)
        
        print(f"✅ 资金流数据导入完成，共 {total_count} 条记录")
    
    def export_stock_info(self):
        """导出股票基本信息到 Elasticsearch"""
        print(f"\n=== 导出股票基本信息 ===")
        
        stock_info_collection = self.mongo_db["stock_info"]
        
        # 添加查询统计
        total_records = stock_info_collection.count_documents({})
        print(f"📊 MongoDB 中股票信息记录数: {total_records}")
        
        if total_records == 0:
            print("⚠️  没有找到股票信息数据")
            return
        
        cursor = stock_info_collection.find()
        
        batch = []
        total_count = 0
        success_count = 0
        skip_count = 0
        
        for doc in cursor:
            try:
                # ✅ 处理 list_date 字段
                list_date = doc.get("list_date")
                processed_list_date = None
                
                if list_date:
                    # 转换为字符串并验证格式
                    list_date_str = str(list_date).strip()
                    
                    # 验证是否为8位数字格式 (YYYYMMDD)
                    if list_date_str.isdigit() and len(list_date_str) == 8:
                        # 验证日期是否有效
                        try:
                            year = int(list_date_str[:4])
                            month = int(list_date_str[4:6])
                            day = int(list_date_str[6:8])
                            
                            # 简单的日期验证
                            if 1900 <= year <= 2030 and 1 <= month <= 12 and 1 <= day <= 31:
                                processed_list_date = list_date_str
                            else:
                                print(f"⚠️  无效日期格式: {list_date_str} for symbol {doc.get('symbol')}")
                        except (ValueError, IndexError):
                            print(f"⚠️  日期解析失败: {list_date_str} for symbol {doc.get('symbol')}")
                    else:
                        print(f"⚠️  日期格式不正确: {list_date_str} for symbol {doc.get('symbol')}")
                
                # ✅ 构建 ES 文档，只有有效日期才包含 list_date 字段
                es_doc = {
                    "symbol": str(doc["symbol"]),
                    "name": str(doc.get("name", "")),
                    "industry": str(doc.get("industry", "")),
                    "area": str(doc.get("area", "")),
                    "market": str(doc.get("market", "")),
                    "total_share": float(doc.get("total_share", 0)) if doc.get("total_share") is not None else 0.0,
                    "flow_share": float(doc.get("flow_share", 0)) if doc.get("flow_share") is not None else 0.0,
                    "timestamp": datetime.now()
                }
                
                # ✅ 只有当日期有效时才添加到文档中
                if processed_list_date:
                    es_doc["list_date"] = processed_list_date
                
                batch.append({
                    "_index": self.indices['stock_info'],
                    "_source": es_doc
                })
                
                if len(batch) >= 1000:
                    batch_success = self._bulk_index(batch)
                    total_count += len(batch)
                    success_count += batch_success
                    batch = []
                    print(f"已处理 {total_count} 条股票信息，成功 {success_count} 条...")
                    
            except Exception as e:
                skip_count += 1
                print(f"⚠️  跳过无效股票信息: {doc.get('symbol', 'Unknown')}, 错误: {e}")
                continue
    
        # 处理最后一批
        if batch:
            batch_success = self._bulk_index(batch)
            total_count += len(batch)
            success_count += batch_success
        
        print(f"✅ 股票信息导入完成")
        print(f"📊 总处理: {total_count} 条，成功: {success_count} 条，跳过: {skip_count} 条")
    
    def _bulk_index(self, batch):
        """批量索引数据"""
        try:
            from elasticsearch.helpers import bulk
            success_count, failed_items = bulk(
                self.es, 
                batch, 
                raise_on_error=False,
                raise_on_exception=False
            )
            
            if failed_items:
                print(f"⚠️  批量索引部分失败: {len(failed_items)} 条记录")
                # 只显示前3个错误
                for i, fail in enumerate(failed_items[:3]):
                    print(f"   错误 {i+1}: {fail}")
            
            return success_count
            
        except Exception as e:
            print(f"❌ 批量索引失败: {e}")
            return 0
    
    def check_elasticsearch_status(self):
        """检查 Elasticsearch 状态"""
        try:
            # 检查集群健康状态
            health = self.es.cluster.health()
            print(f"📊 Elasticsearch 集群状态: {health['status']}")
            print(f"📊 节点数量: {health['number_of_nodes']}")
            print(f"📊 数据节点: {health['number_of_data_nodes']}")
            
            # 检查索引状态
            try:
                indices_response = self.es.cat.indices(format="json")
                print(f"📊 现有索引数量: {len(indices_response)}")
                
                for index in indices_response:
                    if any(pattern in index['index'] for pattern in ['stock-', 'money-']):
                        print(f"   - {index['index']}: {index['docs.count']} 文档, {index['store.size']}")
            except Exception as idx_error:
                print(f"⚠️  无法获取索引列表: {idx_error}")
                    
        except Exception as e:
            print(f"❌ 检查 Elasticsearch 状态失败: {e}")
    
    def export_all_data(self, symbols=None, days=30):
        """导出所有数据"""
        print("🚀 开始导出股票数据到 Elasticsearch...")
        print(f"📅 导出参数: symbols={symbols}, days={days}")
        
        # 检查 Elasticsearch 状态
        self.check_elasticsearch_status()
        
        # 创建索引模板
        print("\n🔧 创建索引模板...")
        self.create_index_templates()
        
        # 导出各类数据
        try:
            self.export_stock_info()
            self.export_stock_prices(symbols, days)
            self.export_stock_scores(days)
            self.export_money_flow(symbols, days)
            
            print("\n✅ 所有数据导出完成！")
            print("🎯 现在可以在 Kibana 中创建可视化图表了")
            
            # 再次检查状态
            print("\n📊 最终状态检查:")
            self.check_elasticsearch_status()
            
        except Exception as e:
            print(f"❌ 数据导出过程中出错: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    try:
        print("🚀 启动股票数据导出程序...")
        exporter = StockDataExporter()
        
        # 导出热门股票的数据
        hot_stocks = ["000002", "000001", "000858", "002415", "300059"]
        
        exporter.export_all_data(
            symbols=hot_stocks,  # 指定股票
            days=30              # 最近30天数据
        )
        
    except Exception as e:
        print(f"❌ 程序执行失败: {e}")
        import traceback
        traceback.print_exc()