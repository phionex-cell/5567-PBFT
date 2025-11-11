# -*- coding: utf-8 -*-
import sys
import threading
import time
from pbft_utils import json_server, json_send

HOST = "127.0.0.1"
client_port = None
primary_host, primary_port = "127.0.0.1", 5000

def banner():
    print(f"✓ Client started at localhost:{client_port}")
    try:
        json_send(primary_host, primary_port, {"type":"CLIENT_HELLO", "host":"127.0.0.1", "port": client_port})
        print(f"✓ Connected to primary {primary_host}:{primary_port}")
    except Exception:
        print("× Cannot connect to primary; please make sure the primary is running")
    print("="*60)
    print("\nCommands:")
    print("  send <k=v,...>  - send a request to the current leader")
    print("  list            - show number of replies received (from all nodes)")
    print("  quit            - exit")
    print("\nclient> ", end="", flush=True)

replies = []
current_txid = None


# def on_msg(msg, addr):
#     t = msg.get("type")
#     if t == "REPLY":
#         replies.append(msg)
#         src = msg.get("from", "unknown")
#         print(f"\n→ REPLY received from {src}... ")
#         print(f"  Total replies so far: {len(replies)} (expected: all nodes will reply)")
#         print("client> ", end="", flush=True)
def on_msg(msg, addr):
    global current_txid, replies
    t = msg.get("type")
    if t == "REPLY":
        txid = msg.get("txid")
        # 若收到新的交易 ID，则清零统计
        if current_txid != txid:
            current_txid = txid
            replies = []  # 清空旧交易计数
            print(f"\n=== New transaction started: {txid} ===")

        replies.append(msg)
        src = msg.get("from", "unknown")
        result = msg.get("result", "?")
        print(f"\n→ REPLY received from {src} ({result})")
        print(f"  Total replies for tx {current_txid}: {len(replies)} (expected: all nodes will reply)")
        print("client> ", end="", flush=True)


def server():
    json_server("127.0.0.1", client_port, on_msg, on_ready=banner)

def repl():
    while True:
        try:
            cmd = input("client> ").strip()
        except (EOFError, KeyboardInterrupt):
            cmd = "quit"
        if not cmd:
            continue
        if cmd.startswith("send "):
            payload = cmd[len("send "):].strip()
            json_send(primary_host, primary_port, {"type":"CLIENT_TX", "data": payload, "from_port": client_port})
            print("→ Submitted to primary")
        elif cmd == "list":
            print(f"Replies received: {len(replies)}")
        elif cmd == "quit":
            print("Bye!")
            time.sleep(0.2)
            break
        else:
            print("Unknown command")

if __name__ == "__main__":
    if len(sys.argv) >= 2:
        try:
            client_port = int(sys.argv[1])
        except Exception:
            client_port = 7000
    else:
        client_port = 7000
    threading.Thread(target=server, daemon=True).start()
    repl()
