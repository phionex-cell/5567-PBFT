# -*- coding: utf-8 -*-
import json
import socket
import threading
import uuid

ENCODING = "utf-8"
BUFSIZE = 65536

def short_uuid():
    return str(uuid.uuid4())[:8]

def json_send(host, port, obj, timeout=3.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        data = (json.dumps(obj) + "\n").encode(ENCODING)
        s.sendall(data)
    finally:
        try:
            s.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        s.close()

def json_server(host, port, handler, on_ready=None):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(128)
    if on_ready:
        on_ready()

    def _loop():
        while True:
            try:
                c, addr = srv.accept()
            except OSError:
                break
            threading.Thread(target=_handle, args=(c, addr), daemon=True).start()

    def _handle(conn, addr):
        conn.settimeout(10.0)
        buff = b""
        try:
            while True:
                data = conn.recv(BUFSIZE)
                if not data:
                    break
                buff += data
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if not buff:
            return
        for line in buff.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode(ENCODING))
                handler(msg, addr)
            except Exception as e:
                print("× Failed to parse incoming data: ", e)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return srv
