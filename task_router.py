"""Task Scheduler and Router

根据任务类型自动分配到合适的 Worker 节点：
- 实时交易 → 私有 Worker
- 分析/回测 → 公有 Worker（优先）或私有 Worker
"""

from typing import List, Dict, Optional
from datetime import datetime
from db import get_db_collections


class TaskRouter:
    """任务路由器 - 智能分配任务到合适的 Worker"""
    
    @staticmethod
    def create_worker_group(
        symbols: List[str],
        user_id: str,
        task_type: str = "realtime",  # realtime / analysis / backtest
        mode: str = "live",
        config: Optional[Dict] = None,
    ) -> Dict:
        """创建 Worker 组并自动分配到合适的节点
        
        Args:
            symbols: 股票代码列表
            user_id: 用户 ID
            task_type: 任务类型 (realtime=实时交易, analysis=分析, backtest=回测)
            mode: 运行模式 (live/backtest/replay)
            config: 额外配置
            
        Returns:
            创建的 worker_group 文档
        """
        collections = get_db_collections()
        worker_nodes = collections["worker_nodes"]
        worker_groups = collections["worker_groups"]
        
        # 根据任务类型选择节点类型
        if task_type == "realtime":
            # 实时交易必须用私有节点
            required_node_type = "private"
        else:
            # 分析/回测优先用公有节点
            required_node_type = "public"
        
        # 查找合适的在线节点
        now = datetime.utcnow()
        five_min_ago = datetime.fromtimestamp(now.timestamp() - 300)
        
        suitable_node = worker_nodes.find_one({
            "node_type": required_node_type,
            "status": "online",
            "last_heartbeat": {"$gte": five_min_ago}
        })
        
        # 如果没有合适的公有节点，降级到私有节点
        if not suitable_node and required_node_type == "public":
            suitable_node = worker_nodes.find_one({
                "node_type": "private",
                "status": "online",
                "last_heartbeat": {"$gte": five_min_ago}
            })
        
        if not suitable_node:
            raise Exception(f"No available {required_node_type} worker node found")
        
        # 生成 group_id
        import uuid
        group_id = f"group-{task_type}-{uuid.uuid4().hex[:8]}"
        
        # 创建 worker group
        group_doc = {
            "group_id": group_id,
            "node_id": suitable_node["node_id"],
            "symbols": symbols,
            "user_id": user_id,
            "mode": mode,
            "task_type": task_type,
            "desired_state": "running",
            "current_state": None,
            "config": config or {},
            "created_at": now,
            "updated_at": now,
        }
        
        worker_groups.insert_one(group_doc)
        
        # 返回结果（去掉 _id）
        group_doc.pop("_id")
        return group_doc
    
    
    @staticmethod
    def get_available_nodes(task_type: str) -> List[Dict]:
        """获取可用于指定任务类型的节点列表"""
        collections = get_db_collections()
        worker_nodes = collections["worker_nodes"]
        
        now = datetime.utcnow()
        five_min_ago = datetime.fromtimestamp(now.timestamp() - 300)
        
        # 根据任务类型确定节点要求
        if task_type == "realtime":
            # 实时任务只能用私有节点
            node_types = ["private"]
        else:
            # 分析/回测可以用公有或私有节点
            node_types = ["public", "private"]
        
        nodes = list(worker_nodes.find({
            "node_type": {"$in": node_types},
            "status": "online",
            "last_heartbeat": {"$gte": five_min_ago}
        }))
        
        # 处理返回数据
        for node in nodes:
            node["_id"] = str(node["_id"])
        
        return nodes
    
    
    @staticmethod
    def assign_task_to_node(
        node_id: str,
        symbols: List[str],
        user_id: str,
        task_type: str,
        mode: str = "live",
        config: Optional[Dict] = None,
    ) -> Dict:
        """手动指定节点分配任务"""
        collections = get_db_collections()
        worker_nodes = collections["worker_nodes"]
        worker_groups = collections["worker_groups"]
        
        # 验证节点存在且在线
        node = worker_nodes.find_one({"node_id": node_id})
        if not node:
            raise Exception(f"Node {node_id} not found")
        
        if node.get("status") != "online":
            raise Exception(f"Node {node_id} is not online")
        
        # 安全检查：实时任务不能分配给公有节点
        if task_type == "realtime" and node.get("node_type") == "public":
            raise Exception("Realtime tasks cannot be assigned to public nodes")
        
        # 生成 group_id
        import uuid
        group_id = f"group-{task_type}-{uuid.uuid4().hex[:8]}"
        
        # 创建 worker group
        now = datetime.utcnow()
        group_doc = {
            "group_id": group_id,
            "node_id": node_id,
            "symbols": symbols,
            "user_id": user_id,
            "mode": mode,
            "task_type": task_type,
            "desired_state": "running",
            "current_state": None,
            "config": config or {},
            "created_at": now,
            "updated_at": now,
        }
        
        worker_groups.insert_one(group_doc)
        
        group_doc.pop("_id")
        return group_doc
