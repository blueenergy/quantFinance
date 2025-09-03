import os
import tushare as ts
from dotenv import load_dotenv
load_dotenv()

# Replace with your own token
ts.set_token(os.getenv("TUSHARE_TOKEN"))
pro = ts.pro_api()

# Example: get daily stock data
df = pro.daily(ts_code='000001.sz', start_date='20250801', end_date='20250821')
print(df)


# df = ts.get_hist_data('600519.SH')  # No .SH suffix, returns basic daily data
# print(df)