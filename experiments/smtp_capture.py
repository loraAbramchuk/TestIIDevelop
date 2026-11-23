"""Loopback-only SMTP sink for disposable acceptance tests, never real mail delivery."""

import json
import os
import socketserver
import threading
from email import policy
from email.parser import BytesParser
from pathlib import Path


class CaptureSMTP:
    def __init__(self, directory, port=1026):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.messages = []
        self.lock = threading.Lock()
        self.delivery_gate = threading.Event()
        self.delivery_gate.set()
        self.received = threading.Event()
        capture = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(15)
                self.wfile.write(b"220 localhost acceptance SMTP\r\n")
                while line := self.rfile.readline(4096):
                    command = line.split(b" ", 1)[0].strip().upper()
                    if command in (b"EHLO", b"HELO"):
                        self.wfile.write(b"250-localhost\r\n250 SIZE 262144\r\n")
                    elif command == b"DATA":
                        self.wfile.write(b"354 End with dot\r\n")
                        chunks, size = [], 0
                        while data := self.rfile.readline(8192):
                            if data == b".\r\n":
                                break
                            if data.startswith(b".."):
                                data = data[1:]
                            size += len(data)
                            if size > 262144:
                                return
                            chunks.append(data)
                        message = BytesParser(policy=policy.default).parsebytes(b"".join(chunks))
                        with capture.lock:
                            capture.messages.append(
                                {
                                    "to": str(message["To"]),
                                    "subject": str(message["Subject"]),
                                    "body": message.get_content(),
                                }
                            )
                            temporary = capture.directory / "inbox.tmp"
                            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                            with os.fdopen(fd, "w") as output:
                                json.dump(capture.messages, output)
                            os.replace(temporary, capture.directory / "inbox.json")
                        capture.received.set()
                        capture.delivery_gate.wait(timeout=20)
                        self.wfile.write(b"250 accepted\r\n")
                    elif command == b"QUIT":
                        self.wfile.write(b"221 bye\r\n")
                        return
                    else:
                        self.wfile.write(b"250 ok\r\n")

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

            def handle_error(self, request, client_address):
                # An intentional killed SMTP client is part of the recovery experiment.
                pass

        self.server = Server(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
