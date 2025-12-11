// MongoDB初始化脚本
// 创建数据库和用户

// 切换到quantfinance数据库
db = db.getSiblingDB('quantfinance');

// 创建应用用户
db.createUser({
  user: 'quantapp',
  pwd: 'your-app-password-change-this',
  roles: [
    {
      role: 'readWrite',
      db: 'quantfinance'
    }
  ]
});

// 创建集合和索引
db.createCollection('users');
db.createCollection('securities_accounts');
db.createCollection('watchlist_strategies');
db.createCollection('watchlists');
db.createCollection('stock_data');
db.createCollection('analysis_history');
db.createCollection('login_logs');
db.createCollection('admin_logs');

// 为用户集合创建索引
db.users.createIndex({ "username": 1 }, { unique: true });
db.securities_accounts.createIndex({ "user_id": 1, "broker": 1, "account_id": 1 }, { unique: true });
db.watchlist_strategies.createIndex({ "user_id": 1, "symbol": 1 }, { unique: true });
db.users.createIndex({ "email": 1 }, { unique: true });
db.users.createIndex({ "is_admin": 1 });
db.users.createIndex({ "is_active": 1 });
db.users.createIndex({ "created_at": 1 });
db.users.createIndex({ "last_login": 1 });

// 为自选股集合创建索引
db.watchlists.createIndex({ "user_id": 1, "symbol": 1 }, { unique: true });
db.watchlists.createIndex({ "symbol": 1 });
db.watchlists.createIndex({ "created_at": 1 });

// 为股票数据集合创建索引
db.stock_data.createIndex({ "symbol": 1, "date": 1 }, { unique: true });
db.stock_data.createIndex({ "symbol": 1 });
db.stock_data.createIndex({ "date": 1 });

// 为分析历史集合创建索引
db.analysis_history.createIndex({ "user_id": 1, "created_at": -1 });
db.analysis_history.createIndex({ "symbol": 1 });

// 为登录日志集合创建索引
db.login_logs.createIndex({ "user_id": 1, "login_time": -1 });
db.login_logs.createIndex({ "username": 1, "login_time": -1 });
db.login_logs.createIndex({ "ip_address": 1, "login_time": -1 });
db.login_logs.createIndex({ "success": 1, "login_time": -1 });
db.login_logs.createIndex({ "login_time": -1 });

// 为管理员操作日志集合创建索引
db.admin_logs.createIndex({ "admin_id": 1, "timestamp": -1 });
db.admin_logs.createIndex({ "admin_username": 1, "timestamp": -1 });
db.admin_logs.createIndex({ "action": 1, "timestamp": -1 });
db.admin_logs.createIndex({ "target_user_id": 1, "timestamp": -1 });
db.admin_logs.createIndex({ "timestamp": -1 });

// 插入一些示例数据 (可选)
db.stock_data.insertMany([
  {
    symbol: "000001",
    name: "平安银行",
    date: new Date(),
    price: 12.50,
    volume: 1000000,
    created_at: new Date()
  },
  {
    symbol: "000002", 
    name: "万科A",
    date: new Date(),
    price: 18.30,
    volume: 800000,
    created_at: new Date()
  }
]);

print("✅ MongoDB初始化完成！");
print("📊 数据库: quantfinance");
print("👤 用户: quantapp");
print("📋 集合: users, watchlists, stock_data, analysis_history, login_logs, admin_logs");
print("🔍 索引创建完成");
print("🛠️ 管理员功能已配置");
