from datetime import datetime
from pymongo.collection import Collection

class QuotaManager:
    def __init__(self, db):
        self.db = db
        # Use a dedicated collection for tracking usage logic
        # Schema: { user_id, period (YYYY-MM or YYYY-Wxx), count, type (vip/free) }
        self.usage_col = db["quota_usage"]

    def _get_period_key(self, user_level: str) -> str:
        now = datetime.now()
        if user_level == "vip":
            return now.strftime("%Y-%m")  # Monthly for VIP
        # Weekly for Free users (Period resets every Monday)
        return now.strftime("%Y-W%W")

    def get_limit(self, user_level: str) -> int:
        if user_level == "vip":
            return 50
        return 2

    def check_quota(self, user_id: str, user_level: str = "free") -> bool:
        limit = self.get_limit(user_level)
        period = self._get_period_key(user_level)
        
        usage = self.usage_col.find_one({
            "user_id": str(user_id),
            "period": period,
            "level": user_level
        })
        
        count = usage.get("count", 0) if usage else 0
        return count < limit

    def consume_quota(self, user_id: str, user_level: str = "free"):
        period = self._get_period_key(user_level)
        self.usage_col.update_one(
            {
                "user_id": str(user_id), 
                "period": period,
                "level": user_level
            },
            {
                "$inc": {"count": 1}, 
                "$setOnInsert": {
                    "created_at": datetime.now(),
                    "updated_at": datetime.now()
                }
            },
            upsert=True
        )

    def get_remaining(self, user_id: str, user_level: str = "free") -> int:
        limit = self.get_limit(user_level)
        period = self._get_period_key(user_level)
        
        usage = self.usage_col.find_one({
            "user_id": str(user_id),
            "period": period,
            "level": user_level
        })
        
        count = usage.get("count", 0) if usage else 0
        return max(0, limit - count)
