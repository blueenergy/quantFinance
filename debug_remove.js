/**
 * 前端调试工具 - 用于测试自选股移除功能
 * 在浏览器控制台中运行这些函数来调试
 */

// 测试移除功能的调试函数
window.debugWatchlistRemove = {
  
  // 1. 测试watchlist服务
  async testWatchlistService() {
    console.log('🔧 测试 watchlist 服务移除功能');
    
    try {
      // 导入watchlist服务（需要确保已经加载）
      const { watchlistService } = await import('/src/services/watchlist.js');
      
      // 测试移除功能
      const result = await watchlistService.removeFromWatchlist('000001');
      console.log('✅ watchlist服务移除成功:', result);
      return result;
    } catch (error) {
      console.error('❌ watchlist服务移除失败:', error);
      throw error;
    }
  },
  
  // 2. 测试直接API调用
  async testDirectApiCall() {
    console.log('🔧 测试直接API调用');
    
    try {
      const token = localStorage.getItem('access_token');
      if (!token) {
        throw new Error('未找到访问令牌，请先登录');
      }
      
      const response = await fetch('http://localhost:3001/api/user/watchlist/remove', {
        method: 'DELETE',
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ symbol: '000002' })
      });
      
      const result = await response.json();
      console.log('✅ 直接API调用成功:', result);
      return result;
    } catch (error) {
      console.error('❌ 直接API调用失败:', error);
      throw error;
    }
  },
  
  // 3. 检查当前状态
  async checkCurrentState() {
    console.log('🔍 检查当前状态');
    
    try {
      const token = localStorage.getItem('access_token');
      console.log('Token存在:', !!token);
      
      if (token) {
        const response = await fetch('http://localhost:3001/api/user/watchlist', {
          headers: { 'Authorization': `Bearer ${token}` }
        });
        const result = await response.json();
        console.log('当前自选股:', result);
      }
      
      const localWatchlist = localStorage.getItem('watchList');
      console.log('本地自选股:', localWatchlist);
      
    } catch (error) {
      console.error('❌ 状态检查失败:', error);
    }
  },
  
  // 4. 模拟前端移除操作
  async simulateRemove(symbol) {
    console.log(`🎯 模拟移除股票: ${symbol}`);
    
    try {
      // 检查是否登录
      const token = localStorage.getItem('access_token');
      const isAuthenticated = !!token;
      console.log('用户已登录:', isAuthenticated);
      
      if (isAuthenticated) {
        // 模拟服务器移除
        const response = await fetch('http://localhost:3001/api/user/watchlist/remove', {
          method: 'DELETE',
          headers: {
            'Authorization': `Bearer ${token}`,
            'Content-Type': 'application/json'
          },
          body: JSON.stringify({ symbol })
        });
        
        const result = await response.json();
        console.log('服务器移除结果:', result);
        
        if (!result.success) {
          throw new Error(result.message);
        }
      } else {
        // 模拟本地移除
        const localList = JSON.parse(localStorage.getItem('watchList') || '[]');
        const newList = localList.filter(s => s !== symbol);
        localStorage.setItem('watchList', JSON.stringify(newList));
        console.log('本地移除完成，新列表:', newList);
      }
      
      console.log(`✅ ${symbol} 移除成功`);
      
    } catch (error) {
      console.error(`❌ ${symbol} 移除失败:`, error);
      throw error;
    }
  }
};

console.log('🛠️ 自选股移除调试工具已加载');
console.log('使用方法:');
console.log('  debugWatchlistRemove.checkCurrentState() - 检查当前状态');
console.log('  debugWatchlistRemove.testDirectApiCall() - 测试API调用');
console.log('  debugWatchlistRemove.simulateRemove("000001") - 模拟移除操作');
