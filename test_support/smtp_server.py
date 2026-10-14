"""Minimal real SMTP server (plaintext, no auth) for transport tests.

Speaks enough RFC 5321 for smtplib: EHLO/HELO, MAIL, RCPT, DATA, RSET,
NOOP, QUIT. Received messages are appended to `server.messages`.
`fail_with` makes every MAIL command answer with that reply (e.g.
"451 try later" for a transient failure, "550 rejected" for permanent).
"""

from __future__ import annotations

import socketserver
import threading
from contextlib import contextmanager


class _Handler(socketserver.StreamRequestHandler):
    def _send(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode("ascii"))

    def handle(self) -> None:
        srv = self.server
        self._send("220 test-smtp ready")
        mail_from, rcpts = None, []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            cmd = raw.decode("utf-8", "replace").strip()
            verb = cmd.split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                self._send("250 test-smtp")
            elif verb == "MAIL":
                srv.mail_attempts += 1
                if srv.fail_with:
                    self._send(srv.fail_with)
                    continue
                mail_from, rcpts = cmd, []
                self._send("250 OK")
            elif verb == "RCPT":
                rcpts.append(cmd)
                self._send("250 OK")
            elif verb == "DATA":
                self._send("354 end with .")
                lines = []
                while True:
                    ln = self.rfile.readline().decode("utf-8", "replace")
                    if ln in (".\r\n", ".\n", ""):
                        break
                    lines.append(ln)
                srv.messages.append({"from": mail_from, "rcpts": rcpts, "data": "".join(lines)})
                self._send("250 queued")
            elif verb in ("RSET", "NOOP"):
                self._send("250 OK")
            elif verb == "QUIT":
                self._send("221 bye")
                return
            else:
                self._send("502 not implemented")


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@contextmanager
def smtp_server(fail_with: str | None = None):
    srv = _Server(("127.0.0.1", 0), _Handler)
    srv.messages = []
    srv.mail_attempts = 0
    srv.fail_with = fail_with
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
