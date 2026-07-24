from pymongo.asynchronous.mongo_client import AsyncMongoClient
from pymongo.errors import ConnectionFailure
from typing import Optional
import logging
from app.core.config import get_settings
from app.domain.utils.error_reporting import safe_exception_summary
from functools import lru_cache

logger = logging.getLogger(__name__)

class MongoDB:
    _CONNECT_TIMEOUT_MS = 5_000
    _SERVER_SELECTION_TIMEOUT_MS = 5_000
    _SOCKET_TIMEOUT_MS = 30_000
    _WAIT_QUEUE_TIMEOUT_MS = 5_000

    def __init__(self):
        self._client: Optional[AsyncMongoClient] = None
        self._settings = get_settings()
    
    async def initialize(self) -> None:
        """Initialize MongoDB connection and Beanie ODM."""
        if self._client is not None:
            return
            
        try:
            timeout_options = {
                "connectTimeoutMS": self._CONNECT_TIMEOUT_MS,
                "serverSelectionTimeoutMS": self._SERVER_SELECTION_TIMEOUT_MS,
                "socketTimeoutMS": self._SOCKET_TIMEOUT_MS,
                "waitQueueTimeoutMS": self._WAIT_QUEUE_TIMEOUT_MS,
            }
            if self._settings.mongodb_username and self._settings.mongodb_password:
                self._client = AsyncMongoClient(
                    self._settings.mongodb_uri,
                    username=self._settings.mongodb_username,
                    password=self._settings.mongodb_password,
                    **timeout_options,
                )
            else:
                self._client = AsyncMongoClient(
                    self._settings.mongodb_uri,
                    **timeout_options,
                )
            await self._client.admin.command('ping')
            logger.info("Successfully connected to MongoDB")
        except ConnectionFailure as e:
            logger.error(
                "Failed to connect to MongoDB: %s", safe_exception_summary(e)
            )
            raise
        except Exception as e:
            logger.error(
                "Failed to initialize MongoDB storage: %s",
                safe_exception_summary(e),
            )
            raise
    
    async def shutdown(self) -> None:
        """Shutdown MongoDB connection."""
        if self._client is not None:
            await self._client.close()
            self._client = None
            logger.info("Disconnected from MongoDB")
        get_mongodb.cache_clear()
    
    @property
    def client(self) -> AsyncMongoClient:
        """Return initialized MongoDB client"""
        if self._client is None:
            raise RuntimeError("MongoDB client not initialized. Call initialize() first.")
        return self._client


@lru_cache()
def get_mongodb() -> MongoDB:
    """Get the MongoDB instance."""
    return MongoDB()
