
import tushare as ts

# Replace with your own token
ts.set_token('1e703f9c93103a9266af23e98e606fc5cfec17649184f04e78e2226d')
pro = ts.pro_api()

# Example: get daily stock data
df = pro.daily(ts_code='000001.sz', start_date='20250801', end_date='20250821')
print(df)


# df = ts.get_hist_data('600519.SH')  # No .SH suffix, returns basic daily data
# print(df)