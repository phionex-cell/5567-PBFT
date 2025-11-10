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
    print("  send <k=v,...>  - send a request to the primary")
    print("  list            - show number of replies received (from all nodes)")
    print("  quit            - exit")
    print("\nclient> ", end="", flush=True)

replies = []

def on_msg(msg, addr):
    t = msg.get("type")
    if t == "REPLY":
        replies.append(msg)
        src = msg.get("from", "unknown")
        print(f"\n→ REPLY received from {src}... ")
        print(f"  Total replies so far: {len(replies)} (expected: all nodes will reply)")
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
    # Accept either `python pbft_client.py 7000` or default to 7000
    if len(sys.argv) >= 2:
        try:
            client_port = int(sys.argv[1])
        except Exception:
            client_port = 7000
    else:
        client_port = 7000
    threading.Thread(target=server, daemon=True).start()
    repl()
