import json
import uuid
import asyncio
import re
from typing import Any, AsyncGenerator, Optional, Tuple
import logging
from app.infrastructure.storage.redis import get_redis
from app.domain.external.message_queue import MessageQueue
from redis.exceptions import ResponseError

logger = logging.getLogger(__name__)

class RedisStreamQueue(MessageQueue):
    """Redis Stream implementation of message queue"""

    _STREAM_ID_PATTERN = re.compile(r"^(?:\$|\d+(?:-\d+)?)$")
    _EMPTY_STREAM_TTL_SECONDS = 24 * 3600
    _DEAD_LETTER_TTL_SECONDS = 7 * 24 * 3600
    _PUT_SCRIPT = r"""
    local id = redis.call('XADD', KEYS[1], '*', 'data', ARGV[1])
    redis.call('PERSIST', KEYS[1])
    return id
    """
    _ACK_DELETE_SCRIPT = r"""
    local acknowledged = redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
    local deleted = redis.call('XDEL', KEYS[1], ARGV[2])
    if redis.call('XLEN', KEYS[1]) == 0 then
      redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
    end
    return acknowledged + deleted
    """
    
    def __init__(self, stream_name: str):
        self._stream_name = stream_name
        self._redis = get_redis()
        self._lock_expire_seconds = 10  # Lock expiration time
    
    async def _acquire_lock(self, lock_key: str, timeout_seconds: int = 5) -> Optional[str]:
        """Acquire distributed lock
        
        Args:
            lock_key: Lock key name
            timeout_seconds: Timeout in seconds for acquiring lock
            
        Returns:
            str: Lock value if acquired successfully, None otherwise
        """
        lock_value = str(uuid.uuid4())
        end_time = timeout_seconds
        
        while end_time > 0:
            # Use SET with NX and EX for atomic lock acquisition
            result = await self._redis.client.set(
                lock_key,
                lock_value,
                nx=True,  # Only set if key doesn't exist
                ex=self._lock_expire_seconds  # Set expiration time
            )
            
            if result:
                return lock_value
            
            # Wait a bit before retrying
            await asyncio.sleep(0.1)
            end_time -= 0.1
        
        return None
    
    async def _release_lock(self, lock_key: str, lock_value: str) -> bool:
        """Release distributed lock
        
        Args:
            lock_key: Lock key name
            lock_value: Lock value for verification
            
        Returns:
            bool: True if lock released successfully, False otherwise
        """
        # Lua script for atomic lock release
        release_script = """
        if redis.call("GET", KEYS[1]) == ARGV[1] then
            return redis.call("DEL", KEYS[1])
        else
            return 0
        end
        """
        
        try:
            script = self._redis.client.register_script(release_script)
            result = await script(keys=[lock_key], args=[lock_value])
            return result == 1
        except Exception:
            return False
    
    async def put(self, message: Any) -> str:
        """Add a message to the stream
        
        Args:
            message: Message to be sent
            
        Returns:
            str: Message ID
        """
        logger.debug("Putting message into Redis stream: stream=%s", self._stream_name)
        # PERSIST is atomic with XADD so a task stream that was empty and
        # scheduled for cleanup cannot expire after accepting new work.
        wire_message = (
            message
            if isinstance(message, (str, bytes, int, float))
            else json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        )
        message_id = await self._redis.client.eval(
            self._PUT_SCRIPT,
            1,
            self._stream_name,
            wire_message,
        )
        return message_id

    @staticmethod
    def _message_data(message_data: Any) -> Any:
        if not isinstance(message_data, dict):
            return None
        return message_data.get("data", message_data.get(b"data"))

    async def _ensure_group(self, group: str) -> None:
        try:
            await self._redis.client.xgroup_create(
                self._stream_name,
                group,
                id="0-0",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def read_group(
        self,
        group: str,
        consumer: str,
        *,
        min_idle_ms: int = 30_000,
        block_ms: Optional[int] = None,
    ) -> Tuple[str, Any]:
        """Read input with Redis consumer-group at-least-once semantics.

        Stale pending entries are reclaimed before new entries, allowing a
        worker crash before Mongo claim to recover without destructive XDEL.
        """
        await self._ensure_group(group)
        claimed = await self._redis.client.xautoclaim(
            self._stream_name,
            group,
            consumer,
            min_idle_time=max(0, int(min_idle_ms)),
            start_id="0-0",
            count=1,
        )
        claimed_messages = claimed[1] if claimed and len(claimed) > 1 else []
        if claimed_messages:
            message_id, message_data = claimed_messages[0]
            return message_id, self._message_data(message_data)

        messages = await self._redis.client.xreadgroup(
            group,
            consumer,
            {self._stream_name: ">"},
            count=1,
            block=block_ms,
        )
        if not messages or not messages[0][1]:
            return None, None
        message_id, message_data = messages[0][1][0]
        return message_id, self._message_data(message_data)

    async def ack(self, group: str, message_id: str) -> bool:
        if not message_id:
            return False
        # Mongo terminal state commits before this call. Once acknowledged,
        # retaining the prompt in Redis adds no recovery value and violates
        # the bounded terminal-retention policy. XACK+XDEL is one retry-safe
        # operation; the empty stream/group key receives a bounded cleanup TTL.
        return bool(
            await self._redis.client.eval(
                self._ACK_DELETE_SCRIPT,
                1,
                self._stream_name,
                group,
                message_id,
                self._EMPTY_STREAM_TTL_SECONDS,
            )
        )

    async def quarantine(
        self, group: str, message_id: str, *, reason: str, payload_digest: str
    ) -> bool:
        """Dead-letter metadata without copying sensitive prompt/tool payloads."""
        dead_letter_stream = f"{self._stream_name}:dead-letter"
        await self._redis.client.xadd(
            dead_letter_stream,
            {
                "source_id": message_id,
                "reason": reason[:128],
                "payload_sha256": payload_digest,
            },
            maxlen=1000,
            approximate=True,
        )
        await self._redis.client.expire(
            dead_letter_stream,
            self._DEAD_LETTER_TTL_SECONDS,
        )
        return await self.ack(group, message_id)
    
    async def get(self, start_id: str = "0", block_ms: Optional[int] = None) -> Tuple[str, Any]:
        """Get a message from the stream
        
        Args:
            start_id: Message ID to start reading from, defaults to "0" meaning from the earliest message
            block_ms: Block time in milliseconds, defaults to None meaning no blocking
            
        Returns:
            Tuple[str, Any]: (Message ID, Message content), returns (None, None) if no message
        """
        # Handle None start_id by using "0" (read from beginning)
        if start_id is None:
            start_id = "0"
        elif not self._STREAM_ID_PATTERN.fullmatch(start_id):
            logger.warning(
                "Invalid Redis stream start id for %s; falling back to 0",
                self._stream_name,
            )
            start_id = "0"
        logger.debug("Getting message from Redis stream: stream=%s", self._stream_name)
            
        # Read new messages
        messages = await self._redis.client.xread(
            {self._stream_name: start_id},
            count=1,
            block=block_ms
        )
        
        if not messages:
            return None, None
            
        # Get message ID and data
        stream_messages = messages[0][1]
        if not stream_messages:
            return None, None
            
        message_id, message_data = stream_messages[0]
        
        try:
            # Try both bytes and string keys for compatibility
            return message_id, self._message_data(message_data)
        except (KeyError, json.JSONDecodeError):
            return None, None
    
    async def get_range(self, start_id: str = "-", end_id: str = "+", count: int = 100) -> AsyncGenerator[Tuple[str, Any], None]:
        """Get messages within a specified range
        
        Args:
            start_id: Start ID, defaults to "-" meaning the earliest message
            end_id: End ID, defaults to "+" meaning the latest message
            count: Maximum number of messages to return
            
        Yields:
            Tuple[str, Any]: (Message ID, Message content)
        """
        messages = await self._redis.client.xrange(self._stream_name, start_id, end_id, count=count)
        
        if not messages:
            return
            
        for message_id, message_data in messages:
            try:
                # Try both bytes and string keys for compatibility
                data = self._message_data(message_data)
                yield message_id, data
            except (KeyError, json.JSONDecodeError):
                continue
    
    async def get_latest_id(self) -> str:
        """Get the latest message ID
        
        Returns:
            str: Latest message ID, returns "0" if no messages
        """
        messages = await self._redis.client.xrevrange(self._stream_name, "+", "-", count=1)
        if not messages:
            return "0"
        return messages[0][0]
    
    async def clear(self) -> None:
        """Clear all messages from the stream"""
        await self._redis.client.xtrim(self._stream_name, 0)
    
    async def is_empty(self) -> bool:
        """Check if the stream is empty"""
        return await self.size() == 0
    
    async def size(self) -> int:
        """Get the number of messages in the stream"""
        info = await self._redis.client.xlen(self._stream_name)
        return info

    async def delete_message(self, message_id: str) -> bool:
        """Delete a specific message from the stream
        
        Args:
            message_id: ID of the message to delete
            
        Returns:
            bool: True if message was deleted successfully, False otherwise
        """
        try:
            await self._redis.client.xdel(self._stream_name, message_id)
            return True
        except Exception:
            return False

    async def pop(self) -> Tuple[str, Any]:
        """Get and remove the first message from the stream using distributed lock
        
        Returns:
            Tuple[str, Any]: (Message ID, Message content), returns (None, None) if stream is empty
        """
        logger.debug(f"Popping message from stream ({self._stream_name})")
        lock_key = f"lock:{self._stream_name}:pop"
        
        # Acquire distributed lock
        lock_value = await self._acquire_lock(lock_key)
        if not lock_value:
            return None, None
        
        try:
            # Get the first message from stream
            messages = await self._redis.client.xrange(self._stream_name, "-", "+", count=1)
            
            if not messages:
                return None, None
            
            message_id, message_data = messages[0]
            
            # Delete the message from stream
            await self._redis.client.xdel(self._stream_name, message_id)
            
            try:
                # Try both bytes and string keys for compatibility
                return message_id, self._message_data(message_data)
            except (KeyError, json.JSONDecodeError):
                logger.warning(
                    "Error parsing message from Redis stream: stream=%s",
                    self._stream_name,
                )
                return None, None
                
        finally:
            # Always release the lock
            await self._release_lock(lock_key, lock_value)
