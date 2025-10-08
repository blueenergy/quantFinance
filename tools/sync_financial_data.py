#!/usr/bin/env python3
"""
财务数据同步工具 - 从 Tushare 导入到本地 MongoDB

用法:
    # 基本用法
    python sync_financial_data.py --symbols 000001.SZ,000002.SZ --period Q --force
    python sync_financial_data.py --all --test --force  # 同步所有股票（测试模式）
    python sync_financial_data.py --recent-ipo 90 --force   # 同步最近90天上市的股票
    
    # 选择性数据收集（推荐）
    python sync_financial_data.py --symbols 000001.SZ --data-types "financial_indicator" --force
    python sync_financial_data.py --symbols 000001.SZ --data-types "income,balance" --force

功能:
- 从 Tushare 获取利润表、资产负债表、现金流量表、财务指标
- 存储到 MongoDB collections: financial_income, financial_balance, financial_cashflow, financial_indicator
- 支持选择性数据收集（--data-types参数）
- 支持增量更新和全量同步
- 自动处理重复数据和错误恢复
- API限频控制和进度跟踪

详细文档: tools/README_sync_financial_data.md
"""

import os
import sys
import argparse
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import pandas as pd
from pymongo import MongoClient, UpdateOne
from dotenv import load_dotenv
import time
from tqdm import tqdm
import tushare as ts

# 添加项目路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
print(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()
ts.set_token(os.getenv("TUSHARE_TOKEN"))  # 请替换为你的真实 token


class FinancialDataSyncer:
    """财务数据同步器"""
    
    def __init__(self, tushare_token: Optional[str] = None, mongo_url: Optional[str] = None):

        self.pro = ts.pro_api()
        
        # 初始化 MongoDB
        mongo_url = mongo_url or os.getenv('MONGO_URL', 'mongodb://localhost:27017')
        self.client = MongoClient(mongo_url)
        self.db = self.client['finance']
        
        # 财务数据集合
        self.income_collection = self.db['financial_income']      # 利润表
        self.balance_collection = self.db['financial_balance']    # 资产负债表  
        self.cashflow_collection = self.db['financial_cashflow']  # 现金流量表
        self.financial_indicator_collection = self.db['financial_indicator']  # 财务指标
        # 新增：daily_basic (估值/日线基础) 集合
        self.daily_basic_collection = self.db['financial_daily_basic']
        
        # 创建索引
        self._create_indexes()
        
        print(f"✅ FinancialDataSyncer 初始化完成")
        print(f"   MongoDB: {mongo_url}")
        print(f"   Database: finance")
    
    def _create_indexes(self):
        """创建必要的索引"""
        indexes = [
            [('ts_code', 1), ('end_date', -1)],  # 按股票代码和报告期查询
            [('end_date', -1)],                   # 按报告期查询
            [('ann_date', -1)],                   # 按公告日期查询
            [('update_flag', 1)]                  # 按更新标志查询
        ]
        
        for collection in [self.income_collection, self.balance_collection, self.cashflow_collection]:
            for index_keys in indexes:
                try:
                    collection.create_index(index_keys)
                except Exception as e:
                    print(f"⚠️ 创建索引失败: {e}")
        # 为 daily_basic 创建单独索引
        try:
            self.daily_basic_collection.create_index([('ts_code', 1), ('trade_date', -1)])
        except Exception as e:
            print(f"⚠️ 创建 daily_basic 索引失败: {e}")
    
    def get_stock_list(self, recent_days: Optional[int] = None) -> List[str]:
        """获取股票列表"""
        try:
            if recent_days:
                # 获取最近上市的股票
                cutoff_date = (datetime.now() - timedelta(days=recent_days)).strftime('%Y%m%d')
                stock_basic = self.pro.stock_basic(
                    exchange='',
                    list_status='L',
                    fields='ts_code,list_date'
                )
                recent_stocks = stock_basic[stock_basic['list_date'] >= cutoff_date]
                return recent_stocks['ts_code'].tolist()
            else:
                # 获取所有上市股票
                stock_basic = self.pro.stock_basic(
                    exchange='',
                    list_status='L',
                    fields='ts_code'
                )
                return stock_basic['ts_code'].tolist()
        except Exception as e:
            print(f"❌ 获取股票列表失败: {e}")
            return []
    
    def sync_financial_data(self, ts_codes: List[str], period: str = 'Q', 
                           start_date: Optional[str] = None, end_date: Optional[str] = None,
                           force_update: bool = False, 
                           data_types: Optional[List[str]] = None) -> Dict[str, Any]:
        """同步财务数据"""
        # 默认同步所有数据类型
        if data_types is None:
            data_types = ['income', 'balance', 'cashflow', 'financial_indicator', 'daily_basic']
        
        stats = {
            'total': len(ts_codes),
            'success': 0,
            'failed': 0,
            'skipped': 0,
            'errors': []
        }
        
        print(f"🚀 开始同步财务数据...")
        print(f"   股票数量: {len(ts_codes)}")
        print(f"   报告期: {period}")
        print(f"   数据类型: {', '.join(data_types)}")
        print(f"   日期范围: {start_date} ~ {end_date}")
        print(f"   强制更新: {force_update}")
        
        for ts_code in tqdm(ts_codes, desc="同步进度"):
            try:
                # 检查是否需要更新
                if not force_update and self._is_data_current(ts_code, period):
                    stats['skipped'] += 1
                    continue
                
                # 根据data_types参数选择性获取财务数据
                income_data = None
                balance_data = None
                cashflow_data = None
                financial_indicator_data = None
                
                if 'income' in data_types:
                    income_data = self._fetch_income_data(ts_code, period, start_date, end_date)
                    
                if 'balance' in data_types:
                    balance_data = self._fetch_balance_data(ts_code, period, start_date, end_date)
                    
                if 'cashflow' in data_types:
                    print(ts_code, period, start_date, end_date)
                    cashflow_data = self._fetch_cashflow_data(ts_code, period, start_date, end_date)
                    
                if 'financial_indicator' in data_types:
                    financial_indicator_data = self._fetch_financial_indicator_data(ts_code, period, start_date, end_date)

                if 'daily_basic' in data_types:
                    daily_basic_data = self._fetch_daily_basic_data(ts_code, start_date, end_date)
                else:
                    daily_basic_data = None

                print(f"\n📊 {ts_code} 财务数据同步成功:")
                print("income_data:", len(income_data) if income_data is not None else "Skipped")
                print("balance_data:", len(balance_data) if balance_data is not None else "Skipped")
                print("cashflow_data:", len(cashflow_data) if cashflow_data is not None else "Skipped")
                print("financial_indicator_data:", len(financial_indicator_data) if financial_indicator_data is not None else "Skipped")
                print("daily_basic:", len(daily_basic_data) if daily_basic_data is not None else "Skipped")
                
                # 存储到数据库
                self._save_financial_data(ts_code, income_data, balance_data, cashflow_data, financial_indicator_data, daily_basic_data)

                stats['success'] += 1
                
                # API 限频控制
                time.sleep(0.1)
                
            except Exception as e:
                stats['failed'] += 1
                error_msg = f"{ts_code}: {str(e)}"
                stats['errors'].append(error_msg)
                print(f"❌ {error_msg}")
                continue
        
        self._print_sync_stats(stats)
        return stats
    
    def _fetch_income_data(self, ts_code: str, period: str, start_date: Optional[str] = None, end_date: Optional[str] = None) -> pd.DataFrame:
        """获取利润表数据"""
        print(ts_code,period,start_date,end_date)
        return self.pro.income(
            ts_code=ts_code,
            start_date=start_date,
            report_type=2,
            end_date=end_date
        )
    
    def _fetch_balance_data(self, ts_code: str, period: str, start_date: Optional[str] = None, end_date: Optional[str] = None) -> pd.DataFrame:
        """获取资产负债表数据"""
        return self.pro.balancesheet(
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date
        )
    
    def _fetch_cashflow_data(self, ts_code: str, period: str, start_date: Optional[str] = None, end_date: Optional[str] = None) -> pd.DataFrame:
        """获取现金流量表数据"""
        return self.pro.cashflow(
            ts_code=ts_code,
            start_date=start_date,
            report_type=2,
            end_date=end_date
        )
    
    def _fetch_financial_indicator_data(self, ts_code: str, period: str, start_date: Optional[str] = None, end_date: Optional[str] = None) -> pd.DataFrame:
        """获取财务指标数据"""
        return self.pro.fina_indicator(
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date
        )
    
    def _fetch_daily_basic_data(self, ts_code: str, start_date: Optional[str] = None, end_date: Optional[str] = None) -> pd.DataFrame:
        """获取 daily_basic（估值/日线基础数据）"""
        try:
            if start_date or end_date:
                df = self.pro.daily_basic(ts_code=ts_code)
                if df is None or df.empty:
                    return pd.DataFrame()
                if start_date:
                    df = df[df['trade_date'] >= start_date]
                if end_date:
                    df = df[df['trade_date'] <= end_date]
                return df.sort_values('trade_date', ascending=False)
            else:
                return self.pro.daily_basic(ts_code=ts_code, limit=30)
        except Exception as e:
            print(f"⚠️ 获取 daily_basic 失败 {ts_code}: {e}")
            return pd.DataFrame()
    def _save_financial_data(self, ts_code: str, income_df: Optional[pd.DataFrame], 
                           balance_df: Optional[pd.DataFrame], cashflow_df: Optional[pd.DataFrame], 
                           financial_indicator_df: Optional[pd.DataFrame], daily_basic_df: Optional[pd.DataFrame]):
        """保存财务数据到数据库"""
        current_time = datetime.now()
        
        print(f"   💾 保存 {ts_code} 财务数据:")
        print(f"      利润表: {len(income_df) if income_df is not None else 'Skipped'} 条")
        print(f"      资产负债表: {len(balance_df) if balance_df is not None else 'Skipped'} 条") 
        print(f"      现金流量表: {len(cashflow_df) if cashflow_df is not None else 'Skipped'} 条")
        print(f"      财务指标: {len(financial_indicator_df) if financial_indicator_df is not None else 'Skipped'} 条")
        
        # 保存利润表
        if income_df is not None and not income_df.empty:
            self._upsert_dataframe(self.income_collection, income_df, current_time)
        elif income_df is not None and income_df.empty:
            print(f"      ⚠️ {ts_code} 利润表数据为空")
        
        # 保存资产负债表
        if balance_df is not None and not balance_df.empty:
            self._upsert_dataframe(self.balance_collection, balance_df, current_time)
        elif balance_df is not None and balance_df.empty:
            print(f"      ⚠️ {ts_code} 资产负债表数据为空")
        
        # 保存现金流量表
        if cashflow_df is not None and not cashflow_df.empty:
            self._upsert_dataframe(self.cashflow_collection, cashflow_df, current_time)
        elif cashflow_df is not None and cashflow_df.empty:
            print(f"      ⚠️ {ts_code} 现金流量表数据为空")

        # 保存财务指标表
        if financial_indicator_df is not None and not financial_indicator_df.empty:
            self._upsert_dataframe(self.financial_indicator_collection, financial_indicator_df, current_time)
        elif financial_indicator_df is not None and financial_indicator_df.empty:
            print(f"      ⚠️ {ts_code} 财务指标数据为空")
        # 保存 daily_basic 表
        if daily_basic_df is not None and not daily_basic_df.empty:
            # daily_basic 使用 trade_date 作为唯一键，调整 upsert 操作
            # 将 DataFrame 的 'trade_date' 字段重命名为 'end_date' 来复用 upsert 接口
            df = daily_basic_df.copy()
            if 'trade_date' in df.columns:
                df = df.rename(columns={'trade_date': 'end_date'})
            self._upsert_dataframe(self.daily_basic_collection, df, current_time)
        elif daily_basic_df is not None and daily_basic_df.empty:
            print(f"      ⚠️ {ts_code} daily_basic 数据为空")
        
    
    def _upsert_dataframe(self, collection, df: pd.DataFrame, sync_time: datetime):
        """将 DataFrame 数据 upsert 到集合中"""
        print(df)
        if df.empty:
            return
        
        operations = []
        for _, row in df.iterrows():
            doc = row.to_dict()
            # 添加同步时间戳
            doc['synced_at'] = sync_time
            doc['data_source'] = 'tushare'
            
            # 处理 NaN 值
            doc = {k: (None if pd.isna(v) else v) for k, v in doc.items()}
            
            # 定义唯一键
            filter_dict = {
                'ts_code': doc['ts_code'],
                'end_date': doc['end_date']
            }
            
            operations.append(
                UpdateOne(
                    filter_dict,
                    {'$set': doc},
                    upsert=True
                )
            )
        
        if operations:
            try:
                result = collection.bulk_write(operations)
                print(f"   📊 {collection.name}: 插入 {result.upserted_count}, 更新 {result.modified_count}")
            except Exception as e:
                print(f"❌ 批量写入 {collection.name} 失败: {e}")
    
    def _is_data_current(self, ts_code: str, period: str) -> bool:
        """检查数据是否是最新的（简单检查，可根据需要完善）"""
        # 检查是否在最近7天内同步过
        week_ago = datetime.now() - timedelta(days=7)
        
        recent_income = self.income_collection.find_one({
            'ts_code': ts_code,
            'synced_at': {'$gte': week_ago}
        })
        
        return recent_income is not None
    
    def _print_sync_stats(self, stats: Dict[str, Any]):
        """打印同步统计信息"""
        print(f"\n📊 同步完成统计:")
        print(f"   总数: {stats['total']}")
        print(f"   成功: {stats['success']}")
        print(f"   失败: {stats['failed']}")
        print(f"   跳过: {stats['skipped']}")
        print(f"   成功率: {(stats['success'] / stats['total'] * 100):.1f}%")
        
        if stats['errors']:
            print(f"\n❌ 错误详情 (前5个):")
            for error in stats['errors'][:5]:
                print(f"   {error}")
            if len(stats['errors']) > 5:
                print(f"   ... 还有 {len(stats['errors']) - 5} 个错误")
    
    def get_local_financial_data(self, ts_code: str, statement_type: str = 'income', 
                                periods: int = 8) -> pd.DataFrame:
        """从本地数据库获取财务数据"""
        collection_map = {
            'income': self.income_collection,
            'balance': self.balance_collection,
            'cashflow': self.cashflow_collection
        }
        
        collection = collection_map.get(statement_type)
        if collection is None:
            raise ValueError(f"不支持的报表类型: {statement_type}")
        
        cursor = collection.find(
            {'ts_code': ts_code},
            sort=[('end_date', -1)],
            limit=periods
        )
        
        docs = list(cursor)
        if docs:
            df = pd.DataFrame(docs)
            # 移除 MongoDB 的 _id 字段
            if '_id' in df.columns:
                df = df.drop('_id', axis=1)
            return df
        else:
            return pd.DataFrame()


def main():
    """命令行入口"""
    parser = argparse.ArgumentParser(description='财务数据同步工具')
    parser.add_argument('--symbols', type=str, help='股票代码列表，逗号分隔 (如: 000001.SZ,600036.SH)')
    parser.add_argument('--all', action='store_true', help='同步所有股票')
    parser.add_argument('--recent-ipo', type=int, help='同步最近N天上市的股票')
    parser.add_argument('--period', choices=['Q', 'Y'], default='Q', help='报告期类型: Q=季报, Y=年报')
    parser.add_argument('--start-date', type=str, help='开始日期 YYYYMMDD')
    parser.add_argument('--end-date', type=str, help='结束日期 YYYYMMDD')
    parser.add_argument('--force', action='store_true', help='强制更新，忽略已有数据')
    parser.add_argument('--test', action='store_true', help='测试模式，只同步前10只股票')
    parser.add_argument('--data-types', type=str, help='数据类型，逗号分隔 (income,balance,cashflow,financial_indicator)，默认全部')
    
    args = parser.parse_args()
    
    try:
        syncer = FinancialDataSyncer()
        
        # 确定要同步的股票列表
        if args.symbols:
            ts_codes = [s.strip() for s in args.symbols.split(',')]
        elif args.recent_ipo:
            ts_codes = syncer.get_stock_list(recent_days=args.recent_ipo)
        elif args.all:
            ts_codes = syncer.get_stock_list()
        else:
            print("❌ 请指定要同步的股票: --symbols, --all, 或 --recent-ipo")
            return
        print(ts_codes)
        if args.test:
            ts_codes = ts_codes[:10]
            print(f"🧪 测试模式，只同步前 {len(ts_codes)} 只股票")
        
        if not ts_codes:
            print("❌ 没有找到要同步的股票")
            return
        
        # 解析数据类型参数
        data_types = None
        if args.data_types:
            data_types = [dtype.strip() for dtype in args.data_types.split(',')]
            # 验证数据类型
            valid_types = ['income', 'balance', 'cashflow', 'financial_indicator','daily_basic']
            invalid_types = [dtype for dtype in data_types if dtype not in valid_types]
            if invalid_types:
                print(f"❌ 无效的数据类型: {', '.join(invalid_types)}")
                print(f"   支持的数据类型: {', '.join(valid_types)}")
                return
        
        # 开始同步
        stats = syncer.sync_financial_data(
            ts_codes=ts_codes,
            period=args.period,
            start_date=args.start_date,
            end_date=args.end_date,
            force_update=args.force,
            data_types=data_types
        )
        
        print(f"\n✅ 财务数据同步完成!")
        
    except Exception as e:
        print(f"❌ 同步失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    main()