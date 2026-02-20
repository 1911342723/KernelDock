"""
WebSocket 路由模块

提供基于 WebSocket 的代码执行接口，支持：
- 实时输出流
- 会话持久连接
- 执行中断

Requirements:
- 11.1: 提供 WebSocket 端点用于代码执行
- 11.2: 支持实时输出推送
- 11.3: 支持执行中断
"""

import asyncio
import json
import logging
import uuid
from typing import Optional, Dict, Any
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from .executor import session_manager, DEFAULT_TIMEOUT
from .services.sandbox_manager import SandboxManager
from .services.execution_queue import ExecutionQueue

logger = logging.getLogger(__name__)

router = APIRouter()


class WSMessage(BaseModel):
    """WebSocket 消息格式"""
    type: str  # request, response, output, error, heartbeat
    id: Optional[str] = None  # 消息 ID，用于请求-响应匹配
    action: Optional[str] = None  # create_session, execute, upload, etc.
    data: Optional[Dict[str, Any]] = None


class ConnectionManager:
    """
    WebSocket 连接管理器
    
    管理所有活跃的 WebSocket 连接，支持：
    - 按 session_id 存储连接
    - 广播消息
    - 连接生命周期管理
    """
    
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.session_to_connection: Dict[str, str] = {}  # session_id -> connection_id
        
    async def connect(self, websocket: WebSocket, connection_id: str):
        """接受新连接"""
        await websocket.accept()
        self.active_connections[connection_id] = websocket
        logger.info(f"WebSocket connected: {connection_id}")
        
    def disconnect(self, connection_id: str):
        """断开连接"""
        if connection_id in self.active_connections:
            del self.active_connections[connection_id]
            # 清理关联的 session
            sessions_to_remove = [
                sid for sid, cid in self.session_to_connection.items() 
                if cid == connection_id
            ]
            for sid in sessions_to_remove:
                del self.session_to_connection[sid]
            logger.info(f"WebSocket disconnected: {connection_id}")
    
    def bind_session(self, session_id: str, connection_id: str):
        """绑定 session 到连接"""
        self.session_to_connection[session_id] = connection_id
        
    async def send_message(self, connection_id: str, message: dict):
        """发送消息到指定连接"""
        if connection_id in self.active_connections:
            websocket = self.active_connections[connection_id]
            try:
                await websocket.send_json(message)
            except RuntimeError as e:
                logger.warning(f"[ConnectionManager] 发送消息失败，连接可能已关闭: {e}")
                # 清理已关闭的连接
                self.disconnect(connection_id)
            
    async def send_to_session(self, session_id: str, message: dict):
        """发送消息到 session 关联的连接"""
        if session_id in self.session_to_connection:
            connection_id = self.session_to_connection[session_id]
            await self.send_message(connection_id, message)


# 全局连接管理器
connection_manager = ConnectionManager()

# 沙箱管理器引用（由 main.py 设置）
_sandbox_manager: Optional[SandboxManager] = None
_execution_queue: Optional[ExecutionQueue] = None


def set_sandbox_manager(manager: SandboxManager):
    """设置沙箱管理器"""
    global _sandbox_manager
    _sandbox_manager = manager


def set_execution_queue(queue: ExecutionQueue):
    """设置执行队列"""
    global _execution_queue
    _execution_queue = queue


async def handle_create_session(
    websocket: WebSocket,
    connection_id: str,
    msg_id: str,
    data: dict
) -> dict:
    """处理创建会话请求"""
    session_id = data.get("session_id") or str(uuid.uuid4())
    
    # 创建沙箱（如果可用）
    sandbox_info = None
    if _sandbox_manager:
        try:
            sandbox_info = await _sandbox_manager.create_sandbox(session_id=session_id)
            logger.info(f"Created sandbox for session: {session_id}")
        except Exception as e:
            logger.warning(f"Failed to create sandbox: {e}")
    
    # 创建会话
    session = session_manager.create_session(session_id)
    
    # 绑定会话到连接
    connection_manager.bind_session(session_id, connection_id)
    
    if sandbox_info and _sandbox_manager:
        session.set_container(
            sandbox_info.container_id,
            _sandbox_manager._docker_client
        )
    
    return {
        "session_id": session.session_id,
        "workspace_dir": session.workspace_dir,
        "data_dir": session.data_dir,
        "output_dir": session.output_dir,
        "sandbox_id": sandbox_info.sandbox_id if sandbox_info else None
    }


async def handle_execute_code(
    websocket: WebSocket,
    connection_id: str,
    msg_id: str,
    data: dict
) -> dict:
    """
    处理代码执行请求

    支持实时输出推送（通过定期发送 output 消息）
    集成执行队列：排队期间每 2 秒推送位置更新
    """
    session_id = data.get("session_id")
    code = data.get("code", "")
    timeout = data.get("timeout", DEFAULT_TIMEOUT)

    logger.info(f"[EXECUTE] 收到执行请求: session={session_id}, code_len={len(code)}, timeout={timeout}")
    logger.info(f"[EXECUTE] 代码内容前200字符: {code[:200]}...")

    if not session_id:
        raise ValueError("session_id is required")

    session = session_manager.get_or_create_session(session_id)
    connection_manager.bind_session(session_id, connection_id)

    # 发送开始执行消息
    try:
        await connection_manager.send_message(connection_id, {
            "type": "status",
            "id": msg_id,
            "data": {"status": "executing", "session_id": session_id}
        })
    except Exception as e:
        logger.warning(f"[EXECUTE] 无法发送 status 消息: {e}")

    # 如果有执行队列，通过队列控制并发
    if _execution_queue:
        ticket = _execution_queue.create_ticket(session_id)

        # 排队期间推送位置更新的后台任务
        async def push_queue_updates():
            try:
                while ticket.status == "queued":
                    status = _execution_queue.get_queue_status(ticket.ticket_id)
                    if status and status.status == "queued":
                        try:
                            await connection_manager.send_message(connection_id, {
                                "type": "queue_status",
                                "id": msg_id,
                                "data": {
                                    "position": status.position,
                                    "estimated_wait_seconds": round(status.estimated_wait_seconds, 1),
                                    "status": "queued",
                                }
                            })
                        except Exception:
                            break
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                pass

        update_task = asyncio.create_task(push_queue_updates())
        try:
            async with _execution_queue.acquire_with_ticket(ticket) as t:
                update_task.cancel()
                # 发送开始执行通知
                try:
                    await connection_manager.send_message(connection_id, {
                        "type": "queue_status",
                        "id": msg_id,
                        "data": {
                            "status": "executing",
                            "waited_seconds": round(t.started_at - t.enqueued_at, 2) if t.started_at else 0,
                        }
                    })
                except Exception:
                    pass
                result = await _do_execute(session_id, session, code, timeout, websocket, connection_id)
                return result
        finally:
            update_task.cancel()
    else:
        return await _do_execute(session_id, session, code, timeout, websocket, connection_id)


async def _do_execute(session_id, session, code, timeout, websocket, connection_id):
    """实际执行代码逻辑（从 handle_execute_code 提取）"""
    # 检查是否使用沙箱
    if _sandbox_manager:
        logger.info(f"[EXECUTE] 检查沙箱管理器...")
        sandbox_info = await _sandbox_manager.get_sandbox_by_session(session_id)
        if sandbox_info:
            try:
                logger.info(f"[EXECUTE] 使用沙箱执行: sandbox_id={sandbox_info.sandbox_id}")
                result = await _sandbox_manager.execute_code(
                    sandbox_id=sandbox_info.sandbox_id,
                    code=code,
                    timeout=timeout
                )
                logger.info(f"[EXECUTE] 沙箱执行完成: success={result.get('success')}")
                return result
            except Exception as e:
                logger.error(f"Sandbox execution failed: {e}")
                # 回退到本地执行

    # 本地执行
    logger.info(f"[EXECUTE] 使用本地执行模式...")

    # 定义输出回调
    async def on_output(text: str):
        await connection_manager.send_message(connection_id, {
            "type": "output",
            "session_id": session_id,
            "data": {"stdout": text}
        })

    import time as _time
    start_time = _time.time()
    result = await session.execute_code(
        code,
        timeout,
        on_stdout=on_output,
        on_stderr=on_output
    )
    elapsed = _time.time() - start_time
    logger.info(f"[EXECUTE] 本地执行完成: success={result.get('success')}, elapsed={elapsed:.2f}s")
    logger.info(f"[EXECUTE] 返回结果 keys: {list(result.keys()) if isinstance(result, dict) else type(result)}")
    if isinstance(result, dict):
        logger.info(f"[EXECUTE] charts={len(result.get('charts', []))}, tables={len(result.get('tables', []))}, output_len={len(result.get('output', ''))}")
    return result


async def handle_upload_file(
    websocket: WebSocket,
    connection_id: str,
    msg_id: str,
    data: dict
) -> dict:
    """处理文件上传请求（Base64 编码）"""
    import base64
    
    session_id = data.get("session_id")
    filename = data.get("filename", "data.csv")
    content_base64 = data.get("content_base64", "")
    
    if not session_id:
        raise ValueError("session_id is required")
    
    session = session_manager.get_or_create_session(session_id)
    connection_manager.bind_session(session_id, connection_id)
    
    # 解码 Base64 内容
    try:
        content = base64.b64decode(content_base64)
    except Exception as e:
        raise ValueError(f"Invalid base64 content: {e}")
    
    # 保存文件
    result = await session.load_file(content, filename)
    return result


async def handle_get_session(
    websocket: WebSocket,
    connection_id: str,
    msg_id: str,
    data: dict
) -> dict:
    """处理获取会话请求"""
    session_id = data.get("session_id")
    if not session_id:
        raise ValueError("session_id is required")
    
    session = session_manager.get_session(session_id)
    if not session:
        raise ValueError(f"Session not found: {session_id}")
    
    response = {
        "session_id": session.session_id,
        "workspace_dir": session.workspace_dir,
        "data_dir": session.data_dir,
        "output_dir": session.output_dir,
        "data_files": session.data_files,
        "created_at": session.created_at.isoformat()
    }
    
    # 添加沙箱信息
    if _sandbox_manager:
        sandbox_info = await _sandbox_manager.get_sandbox_by_session(session_id)
        if sandbox_info:
            response["sandbox_id"] = sandbox_info.sandbox_id
            response["sandbox_state"] = sandbox_info.state.value
    
    return response


async def handle_delete_session(
    websocket: WebSocket,
    connection_id: str,
    msg_id: str,
    data: dict
) -> dict:
    """处理删除会话请求"""
    session_id = data.get("session_id")
    if not session_id:
        raise ValueError("session_id is required")
    
    # 删除沙箱
    if _sandbox_manager:
        sandbox_info = await _sandbox_manager.get_sandbox_by_session(session_id)
        if sandbox_info:
            await _sandbox_manager.destroy_sandbox(sandbox_info.sandbox_id)
    
    # 删除会话
    success = session_manager.delete_session(session_id)
    
    return {"success": success, "session_id": session_id}


# 动作处理器映射
ACTION_HANDLERS = {
    "create_session": handle_create_session,
    "execute": handle_execute_code,
    "upload": handle_upload_file,
    "get_session": handle_get_session,
    "delete_session": handle_delete_session,
}


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket 端点
    
    协议：
    - 客户端发送 JSON 消息：{"type": "request", "id": "xxx", "action": "execute", "data": {...}}
    - 服务端响应 JSON 消息：{"type": "response", "id": "xxx", "success": true, "data": {...}}
    - 服务端推送输出：{"type": "output", "session_id": "xxx", "data": {"stdout": "..."}}
    - 心跳：{"type": "heartbeat"} / {"type": "heartbeat_ack"}
    
    注意：WebSocket 消息大小限制需要在 uvicorn 启动时配置 --ws-max-size
    """
    connection_id = str(uuid.uuid4())
    await connection_manager.connect(websocket, connection_id)
    
    try:
        while True:
            # 接收消息
            raw_message = await websocket.receive_text()
            
            try:
                message = json.loads(raw_message)
            except json.JSONDecodeError:
                try:
                    await websocket.send_json({
                        "type": "error",
                        "error": "Invalid JSON"
                    })
                except RuntimeError:
                    # 连接已关闭
                    break
                continue
            
            msg_type = message.get("type", "request")
            msg_id = message.get("id", str(uuid.uuid4()))
            action = message.get("action")
            data = message.get("data", {})
            
            # 处理心跳
            if msg_type == "heartbeat":
                try:
                    await websocket.send_json({"type": "heartbeat_ack"})
                except RuntimeError:
                    break
                continue
            
            # 处理请求
            if msg_type == "request" and action:
                handler = ACTION_HANDLERS.get(action)
                if not handler:
                    try:
                        await websocket.send_json({
                            "type": "response",
                            "id": msg_id,
                            "success": False,
                            "error": f"Unknown action: {action}"
                        })
                    except RuntimeError:
                        break
                    continue
                
                try:
                    result = await handler(websocket, connection_id, msg_id, data)
                    response = {
                        "type": "response",
                        "id": msg_id,
                        "success": True,
                        "data": result
                    }
                    # 调试日志
                    logger.info(f"[WS] 准备发送响应: action={action}, msg_id={msg_id}")
                    if isinstance(result, dict):
                        logger.info(f"[WS] 响应数据 keys: {list(result.keys())}")
                        logger.info(f"[WS] charts={len(result.get('charts', []))}, tables={len(result.get('tables', []))}, output_len={len(result.get('output', ''))}")
                    try:
                        await websocket.send_json(response)
                        logger.info(f"[WS] 响应已发送成功")
                    except RuntimeError as e:
                        logger.warning(f"[WS] 连接已关闭，无法发送响应: {e}")
                        break
                except Exception as e:
                    logger.error(f"Action {action} failed: {e}", exc_info=True)
                    try:
                        await websocket.send_json({
                            "type": "response",
                            "id": msg_id,
                            "success": False,
                            "error": str(e)
                        })
                    except RuntimeError:
                        logger.warning(f"[WS] 连接已关闭，无法发送错误响应")
                        break
    
    except WebSocketDisconnect:
        connection_manager.disconnect(connection_id)
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
        connection_manager.disconnect(connection_id)


@router.websocket("/ws/{session_id}")
async def websocket_session_endpoint(websocket: WebSocket, session_id: str):
    """
    会话专用 WebSocket 端点
    
    自动绑定到指定会话，简化客户端使用。
    """
    connection_id = str(uuid.uuid4())
    await connection_manager.connect(websocket, connection_id)
    connection_manager.bind_session(session_id, connection_id)
    
    # 确保会话存在
    session = session_manager.get_or_create_session(session_id)
    
    try:
        while True:
            raw_message = await websocket.receive_text()
            
            try:
                message = json.loads(raw_message)
            except json.JSONDecodeError:
                await websocket.send_json({
                    "type": "error",
                    "error": "Invalid JSON"
                })
                continue
            
            msg_type = message.get("type", "request")
            msg_id = message.get("id", str(uuid.uuid4()))
            action = message.get("action")
            data = message.get("data", {})
            
            # 自动添加 session_id
            data["session_id"] = session_id
            
            # 处理心跳
            if msg_type == "heartbeat":
                await websocket.send_json({"type": "heartbeat_ack"})
                continue
            
            # 处理请求
            if msg_type == "request" and action:
                handler = ACTION_HANDLERS.get(action)
                if not handler:
                    await websocket.send_json({
                        "type": "response",
                        "id": msg_id,
                        "success": False,
                        "error": f"Unknown action: {action}"
                    })
                    continue
                
                try:
                    result = await handler(websocket, connection_id, msg_id, data)
                    await websocket.send_json({
                        "type": "response",
                        "id": msg_id,
                        "success": True,
                        "data": result
                    })
                except Exception as e:
                    logger.error(f"Action {action} failed: {e}", exc_info=True)
                    await websocket.send_json({
                        "type": "response",
                        "id": msg_id,
                        "success": False,
                        "error": str(e)
                    })
    
    except WebSocketDisconnect:
        connection_manager.disconnect(connection_id)
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
        connection_manager.disconnect(connection_id)
