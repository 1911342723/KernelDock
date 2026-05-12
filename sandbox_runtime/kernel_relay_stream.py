"""
Kernel Relay Stream — 在容器内把 kernel 的 NDJSON 事件流原样转发到 stdout。
"""

import socket
import struct
import sys


def main() -> None:
    req_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/_kreq.json"

    try:
        with open(req_path, "r", encoding="utf-8") as file:
            request_data = file.read()
    except Exception as exc:
        sys.stdout.write('{"type":"error","error":"读取请求失败: %s"}\n' % str(exc).replace('"', "'"))
        sys.exit(1)

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(600)
        sock.connect(("127.0.0.1", 9999))
    except Exception as exc:
        sys.stdout.write('{"type":"error","error":"Kernel 不可达: %s","kernel_unavailable":true}\n' % str(exc).replace('"', "'"))
        sys.exit(2)

    try:
        payload = request_data.encode("utf-8")
        sock.sendall(struct.pack(">I", len(payload)) + payload)
        while True:
            chunk = sock.recv(8192)
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
        sock.close()
    except Exception as exc:
        sys.stdout.write('{"type":"error","error":"Kernel 通信失败: %s"}\n' % str(exc).replace('"', "'"))
        sys.exit(3)


if __name__ == "__main__":
    main()
