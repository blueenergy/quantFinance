import requests

def get_eastmoney_quote(stock_code):
    # 东方财富股票详情接口
    url = f"https://push2.eastmoney.com/api/qt/stock/get?secid=1.{stock_code}&fields=f57,f58,f43,f44,f45,f46,f47,f48,f49,f50"
    resp = requests.get(url)
    data = resp.json()
    if 'data' in data and data['data']:
        info = data['data']
        return {
            'name': info.get('f58'),
            'code': info.get('f57'),
            'price': info.get('f43'),
            'high': info.get('f44'),
            'low': info.get('f45'),
            'open': info.get('f46'),
            'volume': info.get('f47'),
            'turnover': info.get('f48'),
            'market_cap': info.get('f49'),
            'circulating_market_cap': info.get('f50')
        }
    return None

# 示例
quote = get_eastmoney_quote('600519')
print(quote)