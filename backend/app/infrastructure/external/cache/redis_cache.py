import json
import logging
from typing import Optional, Any
from app.domain.external.cache import Cache
from app.infrastructure.storage.redis import get_redis
from app.domain.utils.error_reporting import safe_exception_summary

logger = logging.getLogger(__name__)


class RedisCache:
    """Redis implementation of Cache interface"""

    _CONSUME_VERIFICATION_CODE_SCRIPT = r"""
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end

local ok, data = pcall(cjson.decode, raw)
if not ok or type(data) ~= 'table' or type(data.code) ~= 'string' then
  redis.call('DEL', KEYS[1])
  return 0
end

local attempts = tonumber(data.attempts) or 0
local max_attempts = tonumber(ARGV[2])
if not max_attempts or max_attempts < 1 or attempts >= max_attempts then
  redis.call('DEL', KEYS[1])
  return 0
end

attempts = attempts + 1
if data.code == ARGV[1] then
  redis.call('DEL', KEYS[1])
  return 1
end

if attempts >= max_attempts then
  redis.call('DEL', KEYS[1])
  return 0
end

local ttl = redis.call('PTTL', KEYS[1])
if ttl <= 0 then
  redis.call('DEL', KEYS[1])
  return 0
end
data.attempts = attempts
redis.call('SET', KEYS[1], cjson.encode(data), 'PX', ttl)
return 0
"""
    
    def __init__(self):
        self.redis_client = get_redis()
    
    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """Store a value with optional TTL"""
        try:
            await self.redis_client.initialize()
            
            # Serialize value to JSON
            serialized_value = json.dumps(value)
            
            if ttl is not None:
                # Set with TTL
                result = await self.redis_client.client.setex(key, ttl, serialized_value)
            else:
                # Set without TTL
                result = await self.redis_client.client.set(key, serialized_value)
            
            return result is not None
            
        except Exception as e:
            logger.error(
                "Failed to set cache value: %s", safe_exception_summary(e)
            )
            return False
    
    async def get(self, key: str) -> Optional[Any]:
        """Retrieve a value from cache"""
        try:
            await self.redis_client.initialize()
            
            value = await self.redis_client.client.get(key)
            if value is None:
                return None
            
            # Deserialize from JSON
            return json.loads(value)
            
        except json.JSONDecodeError:
            logger.error("Failed to deserialize cache value")
            # Delete corrupted data
            await self.delete(key)
            return None
        except Exception as e:
            logger.error(
                "Failed to get cache value: %s", safe_exception_summary(e)
            )
            return None
    
    async def delete(self, key: str) -> bool:
        """Delete a value from cache"""
        try:
            await self.redis_client.initialize()
            
            result = await self.redis_client.client.delete(key)
            return result > 0
            
        except Exception as e:
            logger.error(
                "Failed to delete cache value: %s", safe_exception_summary(e)
            )
            return False
    
    async def exists(self, key: str) -> bool:
        """Check if a key exists in cache"""
        try:
            await self.redis_client.initialize()
            
            result = await self.redis_client.client.exists(key)
            return result > 0
            
        except Exception as e:
            logger.error(
                "Failed to check cache value existence: %s",
                safe_exception_summary(e),
            )
            return False
    
    async def get_ttl(self, key: str) -> Optional[int]:
        """Get the remaining TTL of a key"""
        try:
            await self.redis_client.initialize()
            
            ttl = await self.redis_client.client.ttl(key)
            
            # Redis returns -1 if key exists but has no expiration
            # Redis returns -2 if key doesn't exist
            if ttl == -2:
                return None  # Key doesn't exist
            elif ttl == -1:
                return None  # Key exists but has no expiration
            else:
                return ttl  # TTL in seconds
                
        except Exception as e:
            logger.error(
                "Failed to get cache TTL: %s", safe_exception_summary(e)
            )
            return None
    
    async def keys(self, pattern: str) -> list[str]:
        """Get all keys matching a pattern"""
        try:
            await self.redis_client.initialize()
            
            keys = await self.redis_client.client.keys(pattern)
            return keys if keys else []
            
        except Exception as e:
            logger.error(
                "Failed to list cache keys: %s", safe_exception_summary(e)
            )
            return []
    
    async def clear_pattern(self, pattern: str) -> int:
        """Clear all keys matching a pattern"""
        try:
            await self.redis_client.initialize()
            
            keys = await self.keys(pattern)
            if not keys:
                return 0
            
            result = await self.redis_client.client.delete(*keys)
            return result
            
        except Exception as e:
            logger.error(
                "Failed to clear cache keys: %s", safe_exception_summary(e)
            )
            return 0

    async def consume_verification_code(
        self, key: str, code: str, max_attempts: int
    ) -> bool:
        """Atomically consume one code or count one failed attempt.

        The Lua script is the serialization boundary. A successful code can
        therefore authorize exactly one caller, and concurrent wrong guesses
        cannot overwrite each other's attempt counter.
        """
        try:
            await self.redis_client.initialize()
            result = await self.redis_client.client.eval(
                self._CONSUME_VERIFICATION_CODE_SCRIPT,
                1,
                key,
                code,
                str(max_attempts),
            )
            return int(result or 0) == 1
        except Exception as e:
            # Password-reset authorization fails closed when Redis is
            # unavailable or returns an invalid result.
            logger.error(
                "Failed to atomically consume verification code: %s",
                safe_exception_summary(e),
            )
            return False
