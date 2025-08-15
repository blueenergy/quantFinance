#!/usr/bin/env python3
"""
批量更新剩余股票的名称 - 优化版本
支持指定批次大小和处理数量
"""

import sys
import os
import time
import signal
from datetime import datetime
# 添加父目录到Python路径，以便导入项目模块
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from volume_price_collector import VolumePriceCollector

class BatchStockNameUpdater:
    def __init__(self):
        self.collector = VolumePriceCollector()
        self.stop_flag = False
        
        # 注册信号处理器，支持Ctrl+C优雅停止
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
    
    def signal_handler(self, signum, frame):
        print(f"\n⚠️ 接收到停止信号 {signum}，正在优雅退出...")
        self.stop_flag = True
    
    def get_missing_stocks(self, limit=None):
        """获取没有名字的股票列表"""
        query = {
            "$or": [
                {"stock_name": {"$exists": False}},
                {"stock_name": None},
                {"stock_name": ""}
            ]
        }
        
        missing_stocks = self.collector.collection.distinct("symbol", query)
        
        if limit:
            return sorted(missing_stocks)[:limit]
        else:
            return sorted(missing_stocks)
    
    def update_batch(self, stocks, batch_size=50):
        """批量更新股票名称"""
        print(f"🔄 开始批量更新 {len(stocks)} 只股票的名称")
        print("=" * 60)
        
        total_updated = 0
        success_count = 0
        fail_count = 0
        
        for i in range(0, len(stocks), batch_size):
            if self.stop_flag:
                print(f"\n⚠️ 用户中断，已处理 {total_updated} 只股票")
                break
                
            batch = stocks[i:i + batch_size]
            batch_num = (i // batch_size) + 1
            total_batches = (len(stocks) + batch_size - 1) // batch_size
            
            print(f"\n📦 批次 {batch_num}/{total_batches}: 处理 {len(batch)} 只股票")
            print(f"   股票范围: {batch[0]} ~ {batch[-1]}")
            print("-" * 40)
            
            batch_start_time = time.time()
            
            for j, symbol in enumerate(batch):
                if self.stop_flag:
                    break
                    
                try:
                    print(f"   {j+1:2d}/{len(batch)} 处理 {symbol}...", end=" ")
                    
                    # 获取股票名称
                    stock_name = self.collector.get_stock_name(symbol)
                    
                    if stock_name and stock_name != symbol:
                        # 更新价格记录
                        result = self.collector.collection.update_many(
                            {"symbol": symbol},
                            {"$set": {"stock_name": stock_name}}
                        )
                        
                        print(f"✅ {stock_name} (更新{result.modified_count}条)")
                        success_count += 1
                    else:
                        print(f"❌ 获取名称失败")
                        fail_count += 1
                        
                    total_updated += 1
                    
                    # 每5只股票休息一下，避免API请求过快
                    if (j + 1) % 5 == 0:
                        time.sleep(0.5)
                        
                except Exception as e:
                    print(f"❌ 错误: {str(e)}")
                    fail_count += 1
                    total_updated += 1
            
            batch_time = time.time() - batch_start_time
            print(f"\n   批次用时: {batch_time:.1f}秒")
            print(f"   成功: {success_count}, 失败: {fail_count}")
            
            # 批次间休息
            if not self.stop_flag and batch_num < total_batches:
                print(f"   休息3秒...")
                time.sleep(3)
        
        return total_updated, success_count, fail_count
    
    def run_update(self, limit=None, batch_size=50):
        """运行批量更新"""
        start_time = time.time()
        
        try:
            print(f"🚀 启动批量股票名称更新")
            print(f"   限制数量: {limit if limit else '无限制'}")
            print(f"   批次大小: {batch_size}")
            print(f"   开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            
            # 获取需要更新的股票
            missing_stocks = self.get_missing_stocks(limit)
            
            if not missing_stocks:
                print("✅ 所有股票都已有名称！")
                return
            
            print(f"\n📊 找到 {len(missing_stocks)} 只需要更新名称的股票")
            
            # 执行批量更新
            total_updated, success_count, fail_count = self.update_batch(missing_stocks, batch_size)
            
            # 统计结果
            end_time = time.time()
            total_time = end_time - start_time
            
            print(f"\n" + "=" * 60)
            print(f"📈 更新完成统计:")
            print(f"   总处理数量: {total_updated}")
            print(f"   成功更新: {success_count}")
            print(f"   失败数量: {fail_count}")
            print(f"   成功率: {success_count/total_updated*100:.1f}%" if total_updated > 0 else "   成功率: 0%")
            print(f"   总用时: {total_time:.1f}秒")
            print(f"   平均速度: {total_updated/total_time:.1f}只/秒" if total_time > 0 else "   平均速度: 0只/秒")
            
            # 再次检查覆盖率
            print(f"\n🔍 更新后统计:")
            all_stocks = self.collector.collection.distinct("symbol")
            stocks_with_names = self.collector.collection.distinct("symbol", {
                "stock_name": {"$exists": True, "$ne": None, "$ne": ""}
            })
            
            coverage = len(stocks_with_names) / len(all_stocks) * 100
            print(f"   总股票数: {len(all_stocks)}")
            print(f"   有名称数: {len(stocks_with_names)}")
            print(f"   覆盖率: {coverage:.1f}%")
            
        except Exception as e:
            print(f"❌ 批量更新失败: {str(e)}")
            import traceback
            traceback.print_exc()
        finally:
            self.collector.close()

def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='批量更新股票名称')
    parser.add_argument('--limit', type=int, help='限制处理的股票数量')
    parser.add_argument('--batch-size', type=int, default=50, help='批次大小 (默认: 50)')
    
    args = parser.parse_args()
    
    updater = BatchStockNameUpdater()
    updater.run_update(limit=args.limit, batch_size=args.batch_size)

if __name__ == "__main__":
    main()
