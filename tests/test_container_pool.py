"""
容器池管理器单元测试

测试 ContainerPool 类的功能，包括：
- 容器池初始化和关闭
- 预热容器创建和维护
- 容器获取和释放
- 健康检查和自动补充

Requirements: 7.1, 7.2, 7.3, 7.4
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.infrastructure.container_pool import (
    ContainerPool,
    PooledContainer,
)
from app.infrastructure.docker_client import ContainerInfo, ContainerState, ExecResult
from app.exceptions import SandboxCreationError


class TestPooledContainer:
    """测试 PooledContainer 数据类"""
    
    def test_create_pooled_container(self):
        """测试创建池化容器"""
        now = datetime.now()
        container = PooledContainer(
            container_id="test_container_123",
            created_at=now,
            is_healthy=True,
            last_health_check=now
        )
        
        assert container.container_id == "test_container_123"
        assert container.created_at == now
        assert container.is_healthy is True
        assert container.last_health_check == now
    
    def test_age_seconds(self):
        """测试容器存活时间计算"""
        past = datetime.now() - timedelta(seconds=60)
        container = PooledContainer(
            container_id="test_container",
            created_at=past,
            is_healthy=True,
            last_health_check=datetime.now()
        )
        
        # 存活时间应该约为 60 秒
        assert 59 <= container.age_seconds <= 61

    def test_seconds_since_health_check(self):
        """测试距离上次健康检查的时间"""
        past = datetime.now() - timedelta(seconds=30)
        container = PooledContainer(
            container_id="test_container",
            created_at=datetime.now(),
            is_healthy=True,
            last_health_check=past
        )
        
        # 距离上次健康检查应该约为 30 秒
        assert 29 <= container.seconds_since_health_check <= 31
    
    def test_str_representation(self):
        """测试字符串表示"""
        container = PooledContainer(
            container_id="test_container_123456789",
            created_at=datetime.now(),
            is_healthy=True,
            last_health_check=datetime.now()
        )
        
        str_repr = str(container)
        
        assert "test_contai" in str_repr  # 截断的 ID
        assert "healthy=True" in str_repr


class TestContainerPoolInit:
    """测试 ContainerPool 初始化"""
    
    def test_init_with_defaults(self):
        """测试使用默认值初始化"""
        mock_docker_client = MagicMock()
        
        pool = ContainerPool(docker_client=mock_docker_client)
        
        assert pool.pool_size == 3
        assert pool.available_count == 0
        assert pool.total_count == 0
        assert pool.is_running is False
    
    def test_init_with_custom_values(self):
        """测试使用自定义值初始化"""
        mock_docker_client = MagicMock()
        
        pool = ContainerPool(
            docker_client=mock_docker_client,
            pool_size=5,
            max_container_age_seconds=1800,
            health_check_interval_seconds=30,
            image="custom-image:latest"
        )
        
        assert pool.pool_size == 5
        assert pool._max_container_age_seconds == 1800
        assert pool._health_check_interval_seconds == 30
        assert pool._image == "custom-image:latest"
    
    def test_statistics_property(self):
        """测试统计信息属性"""
        mock_docker_client = MagicMock()
        
        pool = ContainerPool(docker_client=mock_docker_client, pool_size=3)
        
        stats = pool.statistics
        
        assert stats["pool_size"] == 3
        assert stats["available_count"] == 0
        assert stats["total_count"] == 0
        assert stats["leased_count"] == 0
        assert stats["total_acquired"] == 0
        assert stats["total_created"] == 0
        assert stats["total_removed"] == 0


@pytest.fixture
def mock_docker_client():
    """创建模拟的 Docker 客户端"""
    client = AsyncMock()
    counter = {"value": 0}
    
    # 模拟容器创建
    async def create_container(*args, **kwargs):
        counter["value"] += 1
        return ContainerInfo(
            container_id=f"container_{counter['value']}",
            name="test_container",
            image="test-image",
            state=ContainerState.CREATED,
            created_at=datetime.now(),
            started_at=None,
        )
    
    client.create_container = AsyncMock(side_effect=create_container)
    client.start_container = AsyncMock()
    client.stop_container = AsyncMock()
    client.remove_container = AsyncMock()
    client.is_container_running = AsyncMock(return_value=True)
    client.exec_command = AsyncMock(
        return_value=ExecResult(exit_code=0, stdout='{"status":"ok"}', stderr="")
    )
    client.list_containers = AsyncMock(return_value=[])
    
    return client


class TestContainerPoolLifecycle:
    """测试容器池生命周期"""
    
    @pytest.mark.asyncio
    async def test_initialize_creates_warm_containers(self, mock_docker_client):
        """测试初始化时创建预热容器 (Requirements 7.1)"""
        pool = ContainerPool(
            docker_client=mock_docker_client,
            pool_size=3,
            health_check_interval_seconds=3600  # 设置长间隔避免干扰
        )
        
        await pool.initialize()
        
        try:
            # 应该创建了 3 个容器
            assert mock_docker_client.create_container.call_count == 3
            assert mock_docker_client.start_container.call_count == 3
            assert pool.available_count == 3
            assert pool.is_running is True
        finally:
            await pool.shutdown()
    
    @pytest.mark.asyncio
    async def test_shutdown_cleans_up_containers(self, mock_docker_client):
        """测试关闭时清理容器"""
        pool = ContainerPool(
            docker_client=mock_docker_client,
            pool_size=2,
            health_check_interval_seconds=3600
        )
        
        await pool.initialize()
        await pool.shutdown()
        
        # 应该停止并移除所有容器
        assert mock_docker_client.stop_container.call_count >= 2
        assert mock_docker_client.remove_container.call_count >= 2
        assert pool.available_count == 0
        assert pool.is_running is False
    
    @pytest.mark.asyncio
    async def test_context_manager(self, mock_docker_client):
        """测试异步上下文管理器"""
        async with ContainerPool(
            docker_client=mock_docker_client,
            pool_size=2,
            health_check_interval_seconds=3600
        ) as pool:
            assert pool.is_running is True
            assert pool.available_count == 2
        
        # 退出后应该已关闭
        assert pool.is_running is False


class TestContainerAcquisition:
    """测试容器获取"""
    
    @pytest.mark.asyncio
    async def test_acquire_returns_container(self, mock_docker_client):
        """测试获取容器 (Requirements 7.2)"""
        pool = ContainerPool(
            docker_client=mock_docker_client,
            pool_size=2,
            health_check_interval_seconds=3600
        )
        
        await pool.initialize()
        
        try:
            container = await pool.acquire()
            
            assert container is not None
            assert container.container_id is not None
            assert pool.available_count == 1  # 剩余 1 个
        finally:
            await pool.shutdown()
    
    @pytest.mark.asyncio
    async def test_acquire_triggers_replenish(self, mock_docker_client):
        """测试获取容器后触发补充 (Requirements 7.3)"""
        pool = ContainerPool(
            docker_client=mock_docker_client,
            pool_size=2,
            health_check_interval_seconds=3600
        )
        
        await pool.initialize()
        initial_create_count = mock_docker_client.create_container.call_count
        
        try:
            # 获取一个容器
            await pool.acquire()
            
            # 等待补充任务执行
            await asyncio.sleep(0.2)
            
            # 应该触发了新的容器创建
            assert mock_docker_client.create_container.call_count > initial_create_count
        finally:
            await pool.shutdown()


class TestContainerRelease:
    """测试容器释放"""
    
    @pytest.mark.asyncio
    async def test_release_healthy_container(self, mock_docker_client):
        """测试释放健康容器回池"""
        pool = ContainerPool(
            docker_client=mock_docker_client,
            pool_size=2,
            health_check_interval_seconds=3600
        )
        
        await pool.initialize()
        
        try:
            # 获取容器
            container = await pool.acquire()
            assert pool.available_count == 1
            
            # 释放容器
            await pool.release(container.container_id)
            
            # 容器应该回到池中
            assert pool.available_count == 2
        finally:
            await pool.shutdown()

    @pytest.mark.asyncio
    async def test_leased_container_not_reclaimed_by_health_check(self, mock_docker_client):
        """借出的执行容器不应被健康检查误回收。"""
        pool = ContainerPool(
            docker_client=mock_docker_client,
            pool_size=2,
            max_container_age_seconds=1,
            health_check_interval_seconds=3600
        )

        await pool.initialize()

        leased = None
        try:
            leased = await pool.acquire_for_execution()

            assert leased is not None
            assert pool.leased_count == 1
            assert pool.available_count == 1

            await asyncio.sleep(1.5)
            await pool.health_check()

            assert pool.leased_count == 1
            removed_ids = {
                call.args[0]
                for call in mock_docker_client.remove_container.await_args_list
                if call.args
            }
            assert leased.container_id not in removed_ids
        finally:
            if leased is not None:
                await pool.release(leased.container_id)
            await pool.shutdown()


class TestHealthCheck:
    """测试健康检查"""
    
    @pytest.mark.asyncio
    async def test_health_check_removes_unhealthy_containers(self, mock_docker_client):
        """测试健康检查移除不健康容器 (Requirements 7.4)"""
        pool = ContainerPool(
            docker_client=mock_docker_client,
            pool_size=2,
            health_check_interval_seconds=3600
        )
        
        await pool.initialize()
        
        try:
            # 模拟容器变得不健康
            mock_docker_client.is_container_running = AsyncMock(return_value=False)
            
            # 执行健康检查
            await pool.health_check()
            
            # 等待补充
            await asyncio.sleep(0.2)
            
            # 不健康的容器应该被移除并补充
            assert mock_docker_client.remove_container.call_count > 0
        finally:
            await pool.shutdown()
    
    @pytest.mark.asyncio
    async def test_health_check_removes_expired_containers(self, mock_docker_client):
        """测试健康检查移除过期容器"""
        pool = ContainerPool(
            docker_client=mock_docker_client,
            pool_size=2,
            max_container_age_seconds=1,  # 1 秒过期
            health_check_interval_seconds=3600
        )
        
        await pool.initialize()
        
        try:
            # 等待容器过期
            await asyncio.sleep(1.5)
            
            # 执行健康检查
            await pool.health_check()
            
            # 过期的容器应该被移除
            assert mock_docker_client.remove_container.call_count > 0
        finally:
            await pool.shutdown()


class TestPoolStatus:
    """测试容器池状态"""
    
    @pytest.mark.asyncio
    async def test_get_pool_status(self, mock_docker_client):
        """测试获取容器池状态"""
        pool = ContainerPool(
            docker_client=mock_docker_client,
            pool_size=2,
            health_check_interval_seconds=3600
        )
        
        await pool.initialize()
        
        try:
            status = await pool.get_pool_status()
            
            assert status["pool_size"] == 2
            assert status["available_count"] == 2
            assert status["leased_count"] == 0
            assert status["is_running"] is True
            assert "containers" in status
            assert len(status["containers"]) == 2
        finally:
            await pool.shutdown()
