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
db.createCollection('watchlists');
db.createCollection('stock_data');
db.createCollection('analysis_history');

// 为用户集合创建索引
db.users.createIndex({ "username": 1 }, { unique: true });
db.users.createIndex({ "email": 1 }, { unique: true });

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
print("📋 集合: users, watchlists, stock_data, analysis_history");
print("🔍 索引创建完成");
