"""检查 TWS API 连接。"""

import sys
import socket
import argparse


def check_connection(host: str = "127.0.0.1", port: int = 7497, timeout: float = 5.0) -> bool:
    """检查 TWS API Socket 是否可连接。"""
    print(f"检查连接: {host}:{port}")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        if result == 0:
            print(f"[OK] TWS API Socket 可连接 ({host}:{port})")
            return True
        else:
            print(f"[FAIL] 无法连接到 {host}:{port}")
            return False
    except Exception as e:
        print(f"[FAIL] 连接异常: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="检查 TWS API 连接")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7497)
    args = parser.parse_args()

    ok = check_connection(args.host, args.port)

    if not ok:
        print("\n请检查:")
        print("1. TWS 或 IB Gateway 是否已启动")
        print("2. API Socket 是否已启用 (Global Configuration -> API -> Settings)")
        print(f"3. 端口是否设置为 {args.port}")
        print("4. 'Enable ActiveX and Socket Clients' 是否勾选")
        sys.exit(1)
    else:
        print("\n连接正常，可以启动应用。")


if __name__ == "__main__":
    main()
