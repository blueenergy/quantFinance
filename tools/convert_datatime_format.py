import os
from pymongo import MongoClient
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

mongo_url = os.getenv("MONGO_URI", "mongodb://localhost:27017")
client = MongoClient(mongo_url)
db = client["finance"]
collection = db["volume_price"]

def convert_trade_date_format():
    """将 trade_date 从 datetime 格式转换为 YYYYMMDD 字符串格式"""
    
    print("开始转换 trade_date 格式...")
    
    # 1. 先检查需要转换的数据量
    datetime_count = collection.count_documents({"trade_date": {"$type": "date"}})
    string_count = collection.count_documents({"trade_date": {"$type": "string"}})
    
    print(f"发现 {datetime_count} 条 datetime 格式的记录")
    print(f"发现 {string_count} 条 string 格式的记录")
    
    if datetime_count == 0:
        print("没有需要转换的记录")
        return
    
    # 2. 批量转换
    batch_size = 1000
    converted_count = 0
    
    # 查询所有 datetime 格式的记录
    cursor = collection.find({"trade_date": {"$type": "date"}}).batch_size(batch_size)
    
    for doc in cursor:
        try:
            # 获取原始的 datetime 对象
            original_date = doc['trade_date']
            
            # 转换为 YYYYMMDD 字符串格式
            if isinstance(original_date, datetime):
                new_date_str = original_date.strftime('%Y%m%d')
                
                # 更新文档
                result = collection.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"trade_date": new_date_str}}
                )
                
                if result.modified_count > 0:
                    converted_count += 1
                    
                    # 每1000条记录显示一次进度
                    if converted_count % 1000 == 0:
                        print(f"已转换 {converted_count} 条记录...")
                        
        except Exception as e:
            print(f"转换记录失败 {doc.get('_id', 'Unknown')}: {e}")
            continue
    
    print(f"转换完成！共转换 {converted_count} 条记录")
    
    # 3. 验证转换结果
    datetime_count_after = collection.count_documents({"trade_date": {"$type": "date"}})
    string_count_after = collection.count_documents({"trade_date": {"$type": "string"}})
    
    print(f"转换后：{datetime_count_after} 条 datetime 格式，{string_count_after} 条 string 格式")

def verify_conversion():
    """验证转换结果"""
    print("\n=== 验证转换结果 ===")
    
    # 查看转换后的样例数据
    sample_docs = list(collection.find({"symbol": "000002"}).sort("trade_date", -1).limit(5))
    
    print("转换后的样例数据：")
    for doc in sample_docs:
        print(f"Symbol: {doc['symbol']}, Trade Date: {doc['trade_date']} (类型: {type(doc['trade_date'])})")

def create_index_for_new_format():
    """为新格式创建索引"""
    try:
        # 删除旧的索引（如果存在）
        try:
            collection.drop_index("symbol_1_trade_date_1")
        except:
            pass
        
        # 创建新的复合索引
        collection.create_index([("symbol", 1), ("trade_date", -1)], name="symbol_trade_date_idx")
        print("✅ 创建新的索引成功")
        
    except Exception as e:
        print(f"创建索引失败: {e}")

if __name__ == "__main__":
    # 执行转换
    convert_trade_date_format()
    
    # 验证结果
    verify_conversion()
    
    # 创建索引
    create_index_for_new_format()
    
    print("\n转换完成！")