"""
Kernel Relay — 在容器内通过 docker exec 运行的轻量中继脚本

用途：
    宿主机的 CodeExecutor 无法直接 TCP 连接容器（network_mode=none），
    通过 `docker exec python -m sandbox_runtime.kernel_relay /tmp/_kreq.json`
    在容器内部中继请求到 localhost:9999 的 Kernel Server。

退出码：
    0 — 成功，stdout 输出 JSON 响应
    1 — 请求文件读取失败
    2 — Kernel Server 不可达（触发 docker exec 回退）
    3 — 通信失败
"""

import json
import socket
import struct
import sys


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise IOError("连接中断")
        data += chunk
    return data


def main() -> None:
    req_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/_kreq.json"

    # 1. 读取请求文件
    try:
        with open(req_path, "r", encoding="utf-8") as f:
            request_data = f.read()
    except Exception as e:
        json.dump({"success": False, "error": f"读取请求失败: {e}"}, sys.stdout)
        sys.exit(1)

    # 2. 连接 Kernel Server
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(600)
        sock.connect(("127.0.0.1", 9999))
    except Exception as e:
        json.dump(
            {"success": False, "error": f"Kernel 不可达: {e}", "kernel_unavailable": True},
            sys.stdout,
        )
        sys.exit(2)

    # 3. 发送请求 + 接收响应
    try:
        payload = request_data.encode("utf-8")
        sock.sendall(struct.pack(">I", len(payload)) + payload)

        header = _recv_exact(sock, 4)
        resp_len = struct.unpack(">I", header)[0]
        resp_data = _recv_exact(sock, resp_len)
        sock.close()

        sys.stdout.write(resp_data.decode("utf-8"))

    except Exception as e:
        json.dump({"success": False, "error": f"Kernel 通信失败: {e}"}, sys.stdout)
        sys.exit(3)


if __name__ == "__main__":
    main()
