#!/usr/bin/env python3
"""Publish a temporary Discord Rich Presence through the local RPC socket."""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys
import time
import uuid
from pathlib import Path
from typing import Any


OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2
OP_PING = 3
OP_PONG = 4
MAX_FRAME_SIZE = 16 * 1024 * 1024


def encode_frame(opcode: int, payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode()
    return struct.pack("<II", opcode, len(body)) + body


def receive_exact(connection: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = connection.recv(length - len(data))
        if not chunk:
            raise ConnectionError("Discord closed the RPC connection")
        data.extend(chunk)
    return bytes(data)


def receive_frame(connection: socket.socket) -> tuple[int, dict[str, Any]]:
    header = receive_exact(connection, 8)
    opcode, length = struct.unpack("<II", header)
    if length > MAX_FRAME_SIZE:
        raise RuntimeError(f"Discord returned an implausible frame length: {length}")
    body = receive_exact(connection, length)
    return opcode, json.loads(body)


def send_command(
    connection: socket.socket, command: str, arguments: dict[str, Any]
) -> str:
    nonce = str(uuid.uuid4())
    connection.sendall(
        encode_frame(OP_FRAME, {"cmd": command, "args": arguments, "nonce": nonce})
    )
    return nonce


def wait_for_nonce(connection: socket.socket, expected_nonce: str) -> dict[str, Any]:
    while True:
        opcode, message = receive_frame(connection)
        if opcode == OP_PING:
            connection.sendall(encode_frame(OP_PONG, message))
            continue
        if opcode == OP_CLOSE:
            raise RuntimeError(f"Discord rejected the connection: {message}")
        if message.get("nonce") == expected_nonce:
            return message


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--application-id",
        required=True,
        help="public Discord Application ID",
    )
    parser.add_argument(
        "--details",
        default="Manual RPC test",
        help="text displayed beneath the application name",
    )
    args = parser.parse_args()

    if not args.application_id.isdigit():
        parser.error("--application-id must contain only decimal digits")

    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    candidates = [runtime / f"discord-ipc-{number}" for number in range(10)]
    rpc_socket = next((path for path in candidates if path.exists()), None)
    if rpc_socket is None:
        print("Discord's RPC socket was not found. Start Discord first.", file=sys.stderr)
        return 1

    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(str(rpc_socket))
        connection.sendall(
            encode_frame(
                OP_HANDSHAKE,
                {"v": 1, "client_id": args.application_id},
            )
        )
        opcode, ready = receive_frame(connection)
        if opcode == OP_CLOSE:
            raise RuntimeError(f"Discord rejected the Application ID: {ready}")
        if opcode != OP_FRAME or ready.get("evt") != "READY":
            raise RuntimeError(f"Unexpected handshake response: {ready}")

        user = ready.get("data", {}).get("user", {}).get("username")
        print(f"Discord accepted Application ID {args.application_id}.")
        if user:
            print(f"Connected as {user}.")

        nonce = send_command(
            connection,
            "SET_ACTIVITY",
            {
                "pid": os.getpid(),
                "activity": {
                    "details": args.details,
                    "timestamps": {"start": int(time.time())},
                    "instance": True,
                },
            },
        )
        response = wait_for_nonce(connection, nonce)
        if response.get("evt") == "ERROR":
            raise RuntimeError(f"Discord rejected the activity: {response}")

        activity = response.get("data", {})
        print(f"Activity accepted as: {activity.get('name', '(name not returned)')}")
        print("Check your Discord profile now. Press Ctrl+C to clear the activity.")

        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nClearing the test activity...")
        try:
            nonce = send_command(
                connection,
                "SET_ACTIVITY",
                {"pid": os.getpid(), "activity": None},
            )
            wait_for_nonce(connection, nonce)
            print("Activity cleared.")
        except (BrokenPipeError, ConnectionError, OSError, RuntimeError):
            print("Connection closed; Discord should clear the activity automatically.")
    except (ConnectionError, json.JSONDecodeError, OSError, RuntimeError) as exc:
        print(f"Presence test failed: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
