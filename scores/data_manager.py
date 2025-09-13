from pymongo import MongoClient
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta
import tushare as ts
import pandas as pd

load_dotenv()

class DatabaseManager:
    def __init__(self):
        mongo_url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
        self.client = MongoClient(mongo_url)
        self.db = self.client['finance']
        self.stock_scores = self.db['stock_scores']
        self.stock_info = self.db['stock_info']
        self.volume_price = self.db['volume_price']
        self.money_flow = self.db['money_flow']
        
        # 🔧 初始化 Tushare
        ts_token = os.getenv("TUSHARE_TOKEN")
        if ts_token:
            ts.set_token(ts_token)
            self.ts_pro = ts.pro_api()
            print("✅ Tushare API 初始化成功")
        else:
            self.ts_pro = None
            print("⚠️ 未找到 Tushare Token,将使用本地数据")

    def get_stock_info(self, symbol):
        """获取股票基本信息"""
        return self.stock_info.find_one({"symbol": symbol})

    def get_stock_scores(self, symbol, score_date=None):
        """获取股票评分记录"""
        query = {"symbol": symbol}
        if score_date:
            query["score_date"] = score_date
        return list(self.stock_scores.find(query))

    def save_stock_score(self, symbol, score_data, score_date=None):
        """保存股票评分到现有的 stock_scores 表"""
        try:
            from datetime import datetime
            
            if not score_date:
                score_date = datetime.now().strftime('%Y%m%d')
            
            # 🔧 使用现有的 stock_scores 表
            score_record = {
                "symbol": symbol,
                "score_date": score_date,
                "composite_score": score_data.get("composite_score", 0),
                "overall_rating": score_data.get('recommendations',{}).get("overall_rating", "未评级"),
                "valid_scorers":score_data.get("valid_scorers", []),
                
                # 🔧 各维度评分
                "technical_score": score_data.get("scores",{}).get("technical", {}),
                "fundamental_score": score_data.get("scores",{}).get("fundamental", {}),
                "money_flow_score": score_data.get("scores",{}).get("money_flow", {}),
                "growth_score": score_data.get("scores",{}).get("growth", {}),
                "cycle_score": score_data.get("scores",{}).get("cycle", {}),
                "value_score": score_data.get("scores",{}).get("value", {}),

                # 🔧 详细信息存储为JSON字符串
                "details": {
                    "technical_details": score_data.get("technical_analysis", {}),
                    "fundamental_details": score_data.get("fundamental_analysis", {}),
                    "money_flow_details": score_data.get("money_flow_analysis", {}),
                    "growth_details": score_data.get("growth_analysis", {}),
                    "cycle_details": score_data.get("cycle_analysis", {}),
                    "value_details": score_data.get("value_analysis", {}),
                },
                "weights": score_data.get("weights", {}),
                "recommendations": score_data.get("recommendations", {}),
                "updated_at": datetime.now(),
                "algorithm_version": "v0.1"
            }
            
            # 🔧 使用现有表，upsert 避免重复
            result = self.stock_scores.replace_one(
                {"symbol": symbol, "score_date": score_date},
                score_record,
                upsert=True
            )
            
            if result.upserted_id or result.modified_count > 0:
                print(f"✅ {symbol} 评分已保存到 stock_scores 表")
                return True
            else:
                print(f"⚠️ {symbol} 评分保存无变化")
                return True
                
        except Exception as e:
            print(f"❌ 保存评分失败 {symbol}: {e}")
            return False


    def get_volume_price_data(self, symbol, days=None, start_date=None, end_date=None):
        """
        获取价量数据 - 修复 limit 参数类型问题
    
        Args:
            symbol: 股票代码
            days: 最近几天的数据
            start_date: 开始日期
            end_date: 结束日期
        """
        try:
            if days is not None:
                # 🔧 确保 days 是整数类型
                days = int(days)
                return list(self.volume_price.find({
                    "symbol": symbol
                }).sort("trade_date", -1).limit(days))
            elif start_date and end_date:
                # 按日期范围获取
                return list(self.volume_price.find({
                    "symbol": symbol,
                    "trade_date": {
                        "$gte": start_date,
                        "$lte": end_date
                    }
                }).sort("trade_date", 1))
            else:
                # 默认获取最近30天
                return list(self.volume_price.find({
                    "symbol": symbol
                }).sort("trade_date", -1).limit(30))
            
        except Exception as e:
            print(f"⚠️ 获取价量数据失败 {symbol}: {e}")
            return []

    def get_money_flow_data(self, symbol, days=None, start_date=None, end_date=None):
        """
        获取资金流数据
    
        Args:
            symbol: 股票代码
            days: 最近几天的数据
            start_date: 开始日期
            end_date: 结束日期
        """
        try:
            if days is not None:
                # 🔧 确保 days 是整数类型
                days = int(days)
                return list(self.money_flow.find({
                    "symbol": symbol
                }).sort("trade_date", -1).limit(days))
            elif start_date and end_date:
                # 按日期范围获取
                return list(self.money_flow.find({
                    "symbol": symbol,
                    "trade_date": {
                        "$gte": start_date,
                        "$lte": end_date
                    }
                }).sort("trade_date", 1))
            else:
                # 默认获取最近30天
                return list(self.money_flow.find({
                    "symbol": symbol
                }).sort("trade_date", -1).limit(30))
            
        except Exception as e:
            print(f"⚠️ 获取资金流数据失败 {symbol}: {e}")
            return []

    # 🔧 新增：从 Tushare 获取财务数据
    def get_financial_data(self, symbol, periods=1):
        """
        从 Tushare 获取财务数据 - 修复字段名问题
        
        Args:
            symbol: 股票代码 (如 '000001')
            periods: 获取几期数据
            
        Returns:
            单期返回字典，多期返回列表
        """
        if not self.ts_pro:
            print("⚠️ Tushare API 未初始化，返回默认数据")
            default_data = {
                'symbol': symbol,
                'report_date': '20240930',
                'roe': None, 'roa': None, 'gross_margin': None,
                'net_margin': None, 'debt_to_asset': None,
                'current_ratio': None, 'total_revenue': None,
                'net_profit': None, 'pe_ratio': None, 'pb_ratio': None
            }
            return default_data if periods == 1 else [default_data]
        
        try:
            # 转换股票代码格式：000001 -> 000001.SZ
            ts_symbol = self._convert_to_ts_symbol(symbol)
            
            # 🔧 修复字段名，使用正确的 Tushare 字段
            # 获取财务指标数据
            df_fina = self.ts_pro.fina_indicator(
                ts_code=ts_symbol,
                limit=periods,
                fields='ts_code,end_date,roe,roa,grossprofit_margin,netprofit_margin,debt_to_assets,current_ratio'
            )
            
            # 获取利润表数据
            df_income = self.ts_pro.income(
                ts_code=ts_symbol,
                limit=periods,
                fields='ts_code,end_date,total_revenue,revenue,n_income,n_income_attr_p'
            )
            
            # 获取资产负债表数据
            df_balancesheet = self.ts_pro.balancesheet(
                ts_code=ts_symbol,
                limit=periods,
                fields='ts_code,end_date,total_assets,total_liab,total_hldr_eqy_exc_min_int'
            )
            
            # 合并数据
            financial_data = []
            
            # 确保至少有财务指标数据
            if df_fina.empty:
                print(f"⚠️ 未找到 {symbol} 的财务指标数据")
                default_data = {
                    'symbol': symbol,
                    'report_date': '20240930',
                    'roe': None, 'roa': None, 'gross_margin': None,
                    'net_margin': None, 'debt_to_asset': None,
                    'current_ratio': None, 'total_revenue': None,
                    'net_profit': None
                }
                return default_data if periods == 1 else [default_data]
            
            for i in range(len(df_fina)):
                data = {
                    'symbol': symbol,
                    'ts_code': ts_symbol,
                    'report_date': df_fina.iloc[i]['end_date'],
                    
                    # 🔧 使用正确的字段名
                    'roe': df_fina.iloc[i]['roe'] / 100 if pd.notna(df_fina.iloc[i]['roe']) else None,
                    'roa': df_fina.iloc[i]['roa'] / 100 if pd.notna(df_fina.iloc[i]['roa']) else None,
                    'gross_margin': df_fina.iloc[i]['grossprofit_margin'] / 100 if pd.notna(df_fina.iloc[i]['grossprofit_margin']) else None,
                    'net_margin': df_fina.iloc[i]['netprofit_margin'] / 100 if pd.notna(df_fina.iloc[i]['netprofit_margin']) else None,
                    'debt_to_asset': df_fina.iloc[i]['debt_to_assets'] / 100 if pd.notna(df_fina.iloc[i]['debt_to_assets']) else None,
                    'current_ratio': df_fina.iloc[i]['current_ratio'],
                }
                
                # 添加利润表数据（如果有）
                if i < len(df_income):
                    data.update({
                        'total_revenue': df_income.iloc[i]['total_revenue'] * 10000 if pd.notna(df_income.iloc[i]['total_revenue']) else None,
                        'net_profit': df_income.iloc[i]['n_income'] * 10000 if pd.notna(df_income.iloc[i]['n_income']) else None,
                        'net_profit_attr_p': df_income.iloc[i]['n_income_attr_p'] * 10000 if pd.notna(df_income.iloc[i]['n_income_attr_p']) else None,
                    })
                
                # 添加资产负债表数据（如果有）
                if i < len(df_balancesheet):
                    data.update({
                        'total_assets': df_balancesheet.iloc[i]['total_assets'] * 10000 if pd.notna(df_balancesheet.iloc[i]['total_assets']) else None,
                        'total_liabilities': df_balancesheet.iloc[i]['total_liab'] * 10000 if pd.notna(df_balancesheet.iloc[i]['total_liab']) else None,
                        'total_equity': df_balancesheet.iloc[i]['total_hldr_eqy_exc_min_int'] * 10000 if pd.notna(df_balancesheet.iloc[i]['total_hldr_eqy_exc_min_int']) else None,
                    })
                
                financial_data.append(data)
            
            # 为最新一期添加估值数据
            if financial_data:
                try:
                    df_daily_basic = self.ts_pro.daily_basic(
                        ts_code=ts_symbol,
                        limit=1,
                        fields='ts_code,trade_date,pe,pe_ttm,pb,ps,ps_ttm,total_mv,circ_mv,turnover_rate'
                    )
                    
                    if not df_daily_basic.empty:
                        latest_basic = df_daily_basic.iloc[0]
                        financial_data[0].update({
                            'pe_ratio': latest_basic['pe'],
                            'pe_ttm': latest_basic['pe_ttm'],
                            'pb_ratio': latest_basic['pb'],
                            'ps_ratio': latest_basic['ps'],
                            'ps_ttm': latest_basic['ps_ttm'],
                            'total_mv': latest_basic['total_mv'] * 10000 if pd.notna(latest_basic['total_mv']) else None,
                            'circ_mv': latest_basic['circ_mv'] * 10000 if pd.notna(latest_basic['circ_mv']) else None,
                            'turnover_rate': latest_basic['turnover_rate'] / 100 if pd.notna(latest_basic['turnover_rate']) else None,
                        })
                except Exception as e:
                    print(f"⚠️ 获取估值数据失败: {e}")
        
            print(f"✅ 成功获取 {symbol} 的财务数据，共 {len(financial_data)} 期")
            
            if periods == 1:
                return financial_data[0] if financial_data else {}
            else:
                return financial_data
                
        except Exception as e:
            print(f"❌ 从 Tushare 获取财务数据失败 {symbol}: {e}")
            # 返回默认数据
            default_data = {
                'symbol': symbol,
                'report_date': '20240930',
                'roe': None, 'roa': None, 'gross_margin': None,
                'net_margin': None, 'debt_to_asset': None,
                'current_ratio': None, 'total_revenue': None,
                'net_profit': None
            }
            return default_data if periods == 1 else [default_data]

    # 🔧 新增：从 Tushare 获取估值数据
    def get_valuation_data(self, symbol, date=None):
        """
        从 Tushare 获取估值数据 - 修复参数问题
    
        Args:
            symbol: 股票代码
            date: 日期（可选，兼容性参数）
            
        Returns:
            估值数据字典
        """
        if not self.ts_pro:
            print("⚠️ Tushare API 未初始化，返回默认估值数据")
            return {
                'symbol': symbol,
                'pe_ratio': 15.0,
                'pb_ratio': 1.8,
                'ps_ratio': 2.2,
                'total_mv': 50000000000,
                'turnover_rate': 0.03
            }
        
        try:
            # 转换股票代码格式
            ts_symbol = self._convert_to_ts_symbol(symbol)
            
            # 🔧 使用 limit 参数获取最新数据
            df = self.ts_pro.daily_basic(
                ts_code=ts_symbol,
                limit=1,  # 🔧 改用 limit 而不是 trade_date=''
                fields='ts_code,trade_date,pe,pe_ttm,pb,ps,ps_ttm,total_mv,circ_mv,turnover_rate,volume_ratio'
            )
            
            if df.empty:
                print(f"⚠️ 未找到 {symbol} 的估值数据，返回默认值")
                return {
                    'symbol': symbol,
                    'pe_ratio': 15.0,
                    'pb_ratio': 1.8,
                    'ps_ratio': 2.2,
                    'total_mv': 50000000000,
                    'turnover_rate': 0.03
                }
            
            latest_data = df.iloc[0]
            
            valuation_data = {
                'symbol': symbol,
                'ts_code': ts_symbol,
                'trade_date': latest_data['trade_date'],
                'pe_ratio': latest_data['pe'],
                'pe_ttm': latest_data['pe_ttm'],
                'pb_ratio': latest_data['pb'],
                'ps_ratio': latest_data['ps'],
                'ps_ttm': latest_data['ps_ttm'],
                'total_mv': latest_data['total_mv'] * 10000 if pd.notna(latest_data['total_mv']) else None,
                'circ_mv': latest_data['circ_mv'] * 10000 if pd.notna(latest_data['circ_mv']) else None,
                'turnover_rate': latest_data['turnover_rate'] / 100 if pd.notna(latest_data['turnover_rate']) else None,
                'volume_ratio': latest_data['volume_ratio'],
            }
            
            print(f"✅ 成功获取 {symbol} 的估值数据")
            return valuation_data
            
        except Exception as e:
            print(f"❌ 从 Tushare 获取估值数据失败 {symbol}: {e}")
            # 返回默认估值数据，确保流程能继续
            return {
                'symbol': symbol,
                'pe_ratio': 15.0,
                'pb_ratio': 1.8,
                'ps_ratio': 2.2,
                'total_mv': 50000000000,
                'turnover_rate': 0.03
            }

    def get_growth_data(self, symbol, periods=4):
        """获取成长数据（多期财务数据）"""
        return self.get_financial_data(symbol, periods)

    def get_industry_data(self, symbol):
        """获取行业数据"""
        try:
            stock_info = self.get_stock_info(symbol)
            if stock_info:
                return {
                    'industry': stock_info.get('industry', ''),
                    'concept': stock_info.get('concept', ''),
                    'area': stock_info.get('area', ''),
                    'market': stock_info.get('market', ''),
                    'list_date': stock_info.get('list_date', '')
                }
            return {}
        except Exception as e:
            print(f"⚠️ 获取行业数据失败 {symbol}: {e}")
            return {}

    def _convert_to_ts_symbol(self, symbol):
        """
        转换股票代码格式
        
        Args:
            symbol: 原始代码 (如 '000001')
            
        Returns:
            Tushare 格式代码 (如 '000001.SZ')
        """
        if '.' in symbol:
            return symbol  # 已经是正确格式
        
        # 根据代码判断市场
        if symbol.startswith('60') or symbol.startswith('68') or symbol.startswith('51'):
            return f"{symbol}.SH"  # 上海
        elif symbol.startswith('00') or symbol.startswith('30'):
            return f"{symbol}.SZ"  # 深圳
        elif symbol.startswith('8') or symbol.startswith('4'):
            return f"{symbol}.BJ"  # 北京
        else:
            # 默认深圳
            return f"{symbol}.SZ"

    def create_indexes(self):
        """创建必要的数据库索引"""
        try:
            # 股票评分索引
            self.stock_scores.create_index([("symbol", 1), ("score_date", -1)], background=True)
            
            # 价量数据索引
            self.volume_price.create_index([("symbol", 1), ("trade_date", -1)], background=True)
            
            # 资金流数据索引
            self.money_flow.create_index([("symbol", 1), ("trade_date", -1)], background=True)
            
            print("✅ 数据库索引创建完成")
        except Exception as e:
            print(f"❌ 创建索引失败: {e}")

    def get_stock_score_history(self, symbol, days=30):
        """从现有 stock_scores 表获取历史评分"""
        try:
            scores = list(self.db.stock_scores.find(
                {"symbol": symbol}
            ).sort("score_date", -1).limit(days))
            
            print(f"📋 获取 {symbol} 最近 {days} 天评分历史: {len(scores)} 条记录")
            return scores
            
        except Exception as e:
            print(f"❌ 获取历史评分失败 {symbol}: {e}")
            return []

    def check_score_exists(self, symbol, score_date=None):
        """检查现有 stock_scores 表中是否已有评分"""
        try:
            from datetime import datetime
            
            if not score_date:
                score_date = datetime.now().strftime('%Y%m%d')
            
            exists = self.db.stock_scores.find_one({
                "symbol": symbol,
                "score_date": score_date
            })
            
            return exists is not None
            
        except Exception as e:
            print(f"❌ 检查评分存在性失败: {e}")
            return False

    def get_latest_score(self, symbol):
        """获取股票最新评分"""
        try:
            latest = self.db.stock_scores.find_one(
                {"symbol": symbol},
                sort=[("score_date", -1)]
            )
            
            return latest
            
        except Exception as e:
            print(f"❌ 获取最新评分失败 {symbol}: {e}")
            return None

    def get_all_latest_scores(self, min_score=0, limit=100):
        """获取所有股票的最新评分"""
        try:
            # 使用聚合管道获取每只股票的最新评分
            pipeline = [
                {"$sort": {"symbol": 1, "score_date": -1}},
                {"$group": {
                    "_id": "$symbol",
                    "latest_score": {"$first": "$$ROOT"}
                }},
                {"$match": {
                    "latest_score.composite_score": {"$gte": min_score}
                }},
                {"$sort": {"latest_score.composite_score": -1}},
                {"$limit": limit},
                {"$replaceRoot": {"newRoot": "$latest_score"}}
            ]
            
            results = list(self.db.stock_scores.aggregate(pipeline))
            print(f"📊 获取最新评分: {len(results)} 只股票")
            return results
            
        except Exception as e:
            print(f"❌ 获取所有最新评分失败: {e}")
            return []