"""
Wire protocol: every message is a single JSON object followed by '\n'.
Simple, human-inspectable (you can literally `nc` the server and read
what goes by), and trivial to parse without a framework.
"""

import json


def send_json(conn, obj: dict) -> None:
    """Serialize obj as one JSON line and write it to the socket."""
    data = (json.dumps(obj) + "\n").encode("utf-8")
    conn.sendall(data)


def recv_lines(conn):
    """
    Generator that yields parsed JSON objects read from conn, one per
    newline-terminated message. Stops silently (generator exhausts)
    when the peer closes the connection -- callers use this to detect
    disconnects without needing a separate exception handler for EOF.
    """
    buf = b""
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            return  # peer closed the connection
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                # Malformed line from a misbehaving client -- ignore
                # rather than crashing the connection.
                continue
