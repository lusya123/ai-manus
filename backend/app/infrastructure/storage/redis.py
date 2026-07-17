from redis.asyncio import Redis
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError, TimeoutError
from redis.retry import Retry
import logging
from app.core.config import get_settings
from app.domain.utils.error_reporting import safe_exception_summary

logger = logging.getLogger(__name__)

class RedisClient:
    def __init__(self):
        self._client: Redis | None = None
        self._settings = get_settings()
    
    async def initialize(self) -> None:
        """Initialize Redis connection."""
        if self._client is not None:
            return
            
        try:
            # Connect to Redis
            self._client = Redis(
                host=self._settings.redis_host,
                port=self._settings.redis_port,
                db=self._settings.redis_db,
                password=self._settings.redis_password,
                decode_responses=True,
                socket_connect_timeout=self._settings.redis_socket_connect_timeout,
                socket_keepalive=True,
                health_check_interval=self._settings.redis_health_check_interval,
                max_connections=self._settings.redis_max_connections,
                retry=Retry(
                    ExponentialBackoff(cap=1.0, base=0.05),
                    self._settings.redis_retry_attempts,
                ),
                retry_on_error=[ConnectionError, TimeoutError],
            )
            # Verify the connection
            await self._client.ping()
            logger.info("Successfully connected to Redis")
        except Exception as e:
            logger.error(
                "Failed to connect to Redis: %s", safe_exception_summary(e)
            )
            raise
    
    async def shutdown(self) -> None:
        """Shutdown Redis connection."""
        if self._client is not None:
            await self._client.close()
            self._client = None
            logger.info("Disconnected from Redis")
                # Clear cache for this module
        get_redis.cache_clear()
    
    @property
    def client(self) -> Redis:
        """Return initialized Redis client"""
        if self._client is None:
            raise RuntimeError("Redis client not initialized. Call initialize() first.")
        return self._client

from functools import lru_cache

@lru_cache()
def get_redis() -> RedisClient:
    """Get the Redis client instance."""
    return RedisClient()
