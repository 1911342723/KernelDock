"""
会话存储模块

管理沙箱会话状态和元数据，支持会话创建、查询、更新、删除和过期清理。
使用内存存储实现，适用于单实例部署场景。

Requirements:
- 8.1: 生成唯一的会话 ID 并关联到沙箱
- 8.2: 存储会话的元数据（创建时间、最后活动时间、数据文件列表）
- 8.3: 会话超过配置的最大空闲时间时标记为过期
- 8.4: 支持通过会话 ID 恢复之前的沙箱状态
- 8.5: 提供会话列表查询接口，支持分页
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SessionInfo:
    """
    会话信息数据类
    
    存储会话的完整状态信息，包括关联的沙箱、时间戳和元数据。
    
    Attributes:
        session_id: 会话唯一标识符
        sandbox_id: 关联的沙箱 ID（可选，会话创建时可能尚未关联沙箱）
        created_at: 会话创建时间
        last_activity: 最后活动时间
        data_files: 会话中上传的数据文件列表
        metadata: 会话元数据字典
    """
    session_id: str
    sandbox_id: Optional[str]
    created_at: datetime
    last_activity: datetime
    data_files: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def is_expired(
        self, 
        max_idle_seconds: int, 
        max_session_seconds: int
    ) -> bool:
        """
        检查会话是否已过期
        
        会话在以下情况下被视为过期：
        1. 空闲时间超过 max_idle_seconds
        2. 总时长超过 max_session_seconds
        
        Args:
            max_idle_seconds: 最大空闲时间（秒）
            max_session_seconds: 最大会话时长（秒）
            
        Returns:
            True 如果会话已过期，否则 False
        """
        now = datetime.now(timezone.utc)
        
        # 检查空闲时间
        idle_seconds = (now - self.last_activity).total_seconds()
        if idle_seconds > max_idle_seconds:
            return True
        
        # 检查总时长
        total_seconds = (now - self.created_at).total_seconds()
        if total_seconds > max_session_seconds:
            return True
        
        return False
    
    def to_dict(self) -> Dict[str, Any]:
        """
        转换为字典格式
        
        Returns:
            包含会话信息的字典
        """
        return {
            "session_id": self.session_id,
            "sandbox_id": self.sandbox_id,
            "created_at": self.created_at.isoformat(),
            "last_activity": self.last_activity.isoformat(),
            "data_files": self.data_files.copy(),
            "metadata": self.metadata.copy(),
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionInfo":
        """
        从字典创建 SessionInfo 实例
        
        Args:
            data: 包含会话信息的字典
            
        Returns:
            SessionInfo 实例
        """
        return cls(
            session_id=data["session_id"],
            sandbox_id=data.get("sandbox_id"),
            created_at=datetime.fromisoformat(data["created_at"]),
            last_activity=datetime.fromisoformat(data["last_activity"]),
            data_files=data.get("data_files", []).copy(),
            metadata=data.get("metadata", {}).copy(),
        )


class SessionStore:
    """
    会话存储
    
    管理沙箱会话状态和元数据，提供会话的 CRUD 操作和过期清理功能。
    使用内存字典存储会话数据，适用于单实例部署。
    
    Requirements:
    - 8.1: 生成唯一的会话 ID 并关联到沙箱
    - 8.2: 存储会话的元数据
    - 8.3: 会话过期检测
    - 8.4: 通过会话 ID 恢复沙箱状态
    - 8.5: 分页查询接口
    
    Attributes:
        max_idle_seconds: 最大空闲时间（秒），默认 3600（1小时）
        max_session_seconds: 最大会话时长（秒），默认 43200（12小时）
    """
    
    def __init__(
        self,
        max_idle_seconds: int = 3600,
        max_session_seconds: int = 43200
    ):
        """
        初始化会话存储
        
        Args:
            max_idle_seconds: 最大空闲时间（秒），默认 1 小时
            max_session_seconds: 最大会话时长（秒），默认 12 小时
        """
        self._max_idle_seconds = max_idle_seconds
        self._max_session_seconds = max_session_seconds
        
        # 内存存储：session_id -> SessionInfo
        self._sessions: Dict[str, SessionInfo] = {}
        
        # 用于保护并发访问的锁
        self._lock = asyncio.Lock()
        
        logger.info(
            f"会话存储初始化完成: max_idle={max_idle_seconds}s, "
            f"max_session={max_session_seconds}s"
        )
    
    @property
    def max_idle_seconds(self) -> int:
        """获取最大空闲时间（秒）"""
        return self._max_idle_seconds
    
    @property
    def max_session_seconds(self) -> int:
        """获取最大会话时长（秒）"""
        return self._max_session_seconds
    
    def _generate_session_id(self) -> str:
        """
        生成唯一的会话 ID
        
        使用 UUID4 生成随机唯一标识符。
        
        Returns:
            唯一的会话 ID 字符串
        """
        return str(uuid.uuid4())
    
    async def create_session(
        self,
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> SessionInfo:
        """
        创建新会话
        
        生成唯一的会话 ID（如果未提供），初始化会话状态。
        
        Requirements: 8.1, 8.2
        
        Args:
            session_id: 可选的会话 ID，为空则自动生成
            metadata: 可选的会话元数据
            
        Returns:
            创建的 SessionInfo 对象
        """
        async with self._lock:
            # 生成或使用提供的会话 ID
            if session_id is None:
                session_id = self._generate_session_id()
            elif session_id in self._sessions:
                # 如果提供的 ID 已存在，生成新的 ID
                logger.warning(
                    f"会话 ID {session_id} 已存在，生成新的 ID"
                )
                session_id = self._generate_session_id()
            
            now = datetime.now(timezone.utc)
            
            session = SessionInfo(
                session_id=session_id,
                sandbox_id=None,
                created_at=now,
                last_activity=now,
                data_files=[],
                metadata=metadata.copy() if metadata else {},
            )
            
            self._sessions[session_id] = session
            
            logger.info(f"创建会话: {session_id}")
            
            return session
    
    async def get_session(self, session_id: str) -> Optional[SessionInfo]:
        """
        获取会话信息
        
        通过会话 ID 查询会话状态，用于恢复之前的沙箱状态。
        
        Requirements: 8.4
        
        Args:
            session_id: 会话 ID
            
        Returns:
            SessionInfo 对象，如果不存在则返回 None
        """
        async with self._lock:
            session = self._sessions.get(session_id)
            
            if session is None:
                logger.debug(f"会话不存在: {session_id}")
                return None
            
            # 检查是否过期
            if session.is_expired(
                self._max_idle_seconds, 
                self._max_session_seconds
            ):
                logger.info(f"会话已过期: {session_id}")
                # 不自动删除，由 cleanup_expired 处理
                return None
            
            return session
    
    async def update_activity(self, session_id: str) -> bool:
        """
        更新会话活动时间
        
        每次会话有活动时调用，更新 last_activity 时间戳。
        
        Requirements: 8.2
        
        Args:
            session_id: 会话 ID
            
        Returns:
            True 如果更新成功，False 如果会话不存在
        """
        async with self._lock:
            session = self._sessions.get(session_id)
            
            if session is None:
                logger.warning(f"更新活动时间失败，会话不存在: {session_id}")
                return False
            
            session.last_activity = datetime.now(timezone.utc)
            logger.debug(f"更新会话活动时间: {session_id}")
            
            return True
    
    async def update_sandbox_id(
        self, 
        session_id: str, 
        sandbox_id: str
    ) -> bool:
        """
        更新会话关联的沙箱 ID
        
        将会话与沙箱关联，用于会话恢复。
        
        Requirements: 8.1
        
        Args:
            session_id: 会话 ID
            sandbox_id: 沙箱 ID
            
        Returns:
            True 如果更新成功，False 如果会话不存在
        """
        async with self._lock:
            session = self._sessions.get(session_id)
            
            if session is None:
                logger.warning(
                    f"更新沙箱 ID 失败，会话不存在: {session_id}"
                )
                return False
            
            session.sandbox_id = sandbox_id
            session.last_activity = datetime.now(timezone.utc)
            logger.info(f"会话 {session_id} 关联沙箱: {sandbox_id}")
            
            return True
    
    async def add_data_file(
        self,
        session_id: str,
        filename: str
    ) -> bool:
        """
        添加数据文件记录
        
        记录会话中上传的数据文件，用于会话恢复时重新加载。
        
        Requirements: 8.2
        
        Args:
            session_id: 会话 ID
            filename: 数据文件名
            
        Returns:
            True 如果添加成功，False 如果会话不存在
        """
        async with self._lock:
            session = self._sessions.get(session_id)
            
            if session is None:
                logger.warning(
                    f"添加数据文件失败，会话不存在: {session_id}"
                )
                return False
            
            if filename not in session.data_files:
                session.data_files.append(filename)
                logger.debug(
                    f"会话 {session_id} 添加数据文件: {filename}"
                )
            
            session.last_activity = datetime.now(timezone.utc)
            
            return True
    
    async def remove_data_file(
        self,
        session_id: str,
        filename: str
    ) -> bool:
        """
        移除数据文件记录
        
        从会话的数据文件列表中移除指定文件。
        
        Args:
            session_id: 会话 ID
            filename: 数据文件名
            
        Returns:
            True 如果移除成功，False 如果会话不存在或文件不在列表中
        """
        async with self._lock:
            session = self._sessions.get(session_id)
            
            if session is None:
                logger.warning(
                    f"移除数据文件失败，会话不存在: {session_id}"
                )
                return False
            
            if filename in session.data_files:
                session.data_files.remove(filename)
                logger.debug(
                    f"会话 {session_id} 移除数据文件: {filename}"
                )
                return True
            
            return False
    
    async def update_metadata(
        self,
        session_id: str,
        metadata: Dict[str, Any]
    ) -> bool:
        """
        更新会话元数据
        
        合并新的元数据到现有元数据中。
        
        Requirements: 8.2
        
        Args:
            session_id: 会话 ID
            metadata: 要更新的元数据字典
            
        Returns:
            True 如果更新成功，False 如果会话不存在
        """
        async with self._lock:
            session = self._sessions.get(session_id)
            
            if session is None:
                logger.warning(
                    f"更新元数据失败，会话不存在: {session_id}"
                )
                return False
            
            session.metadata.update(metadata)
            session.last_activity = datetime.now(timezone.utc)
            logger.debug(f"会话 {session_id} 更新元数据")
            
            return True
    
    async def delete_session(self, session_id: str) -> bool:
        """
        删除会话
        
        从存储中移除会话记录。
        
        Args:
            session_id: 会话 ID
            
        Returns:
            True 如果删除成功，False 如果会话不存在
        """
        async with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                logger.info(f"删除会话: {session_id}")
                return True
            
            logger.warning(f"删除会话失败，会话不存在: {session_id}")
            return False
    
    async def list_sessions(
        self,
        limit: int = 100,
        offset: int = 0
    ) -> List[SessionInfo]:
        """
        列出会话（支持分页）
        
        返回所有未过期会话的列表，按创建时间降序排列。
        
        Requirements: 8.5
        
        Args:
            limit: 返回的最大会话数，默认 100
            offset: 跳过的会话数，默认 0
            
        Returns:
            SessionInfo 对象列表
        """
        async with self._lock:
            # 过滤未过期的会话
            active_sessions = [
                session for session in self._sessions.values()
                if not session.is_expired(
                    self._max_idle_seconds,
                    self._max_session_seconds
                )
            ]
            
            # 按创建时间降序排列（最新的在前）
            active_sessions.sort(
                key=lambda s: s.created_at,
                reverse=True
            )
            
            # 应用分页
            start = offset
            end = offset + limit
            
            return active_sessions[start:end]
    
    async def cleanup_expired(self) -> int:
        """
        清理过期会话
        
        检测并删除所有过期的会话。
        
        Requirements: 8.3
        
        Returns:
            清理的会话数量
        """
        async with self._lock:
            expired_ids = []
            
            for session_id, session in self._sessions.items():
                if session.is_expired(
                    self._max_idle_seconds,
                    self._max_session_seconds
                ):
                    expired_ids.append(session_id)
            
            for session_id in expired_ids:
                del self._sessions[session_id]
                logger.info(f"清理过期会话: {session_id}")
            
            if expired_ids:
                logger.info(f"共清理 {len(expired_ids)} 个过期会话")
            
            return len(expired_ids)
    
    async def get_session_count(self) -> int:
        """
        获取当前会话总数
        
        Returns:
            会话总数（包括可能已过期但未清理的会话）
        """
        async with self._lock:
            return len(self._sessions)
    
    async def get_active_session_count(self) -> int:
        """
        获取活跃会话数量
        
        Returns:
            未过期的会话数量
        """
        async with self._lock:
            count = 0
            for session in self._sessions.values():
                if not session.is_expired(
                    self._max_idle_seconds,
                    self._max_session_seconds
                ):
                    count += 1
            return count
    
    async def clear_all(self) -> int:
        """
        清除所有会话
        
        用于测试或服务重置。
        
        Returns:
            清除的会话数量
        """
        async with self._lock:
            count = len(self._sessions)
            self._sessions.clear()
            logger.info(f"清除所有会话，共 {count} 个")
            return count


# 全局会话存储单例
_session_store: Optional[SessionStore] = None


def get_session_store() -> SessionStore:
    """
    获取全局会话存储单例
    
    Returns:
        SessionStore 实例
    """
    global _session_store
    if _session_store is None:
        from ..config import settings
        _session_store = SessionStore(
            max_idle_seconds=settings.timeout.session_idle_timeout,
            max_session_seconds=settings.timeout.session_max_timeout,
        )
    return _session_store


def reset_session_store() -> None:
    """
    重置全局会话存储单例
    
    用于测试场景，重新创建会话存储实例。
    """
    global _session_store
    _session_store = None
