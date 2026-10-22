"""Minimal real SMTP server (plaintext, no auth) for transport tests.

Speaks enough RFC 5321 for smtplib: EHLO/HELO, MAIL, RCPT, DATA, RSET,
NOOP, QUIT. Received messages are appended to `server.messages`.
`fail_with` makes every MAIL command answer with that reply (e.g.
"451 try later" for a transient failure, "550 rejected" for permanent).

Size / attachment behaviour (plan B4):
  size_limit      advertise `SIZE n` in EHLO (0 = "no limit"); a MAIL FROM declaring a larger
                  SIZE= is answered "552 5.3.4 message size exceeds fixed limit". None = no SIZE.
  reject_attachments  reply with this line after DATA when the message has an attachment
                  (a content filter refusing .zip), e.g. "554 5.7.1 attachment refused".
  hangup_over     close the connection once DATA has received more than this many bytes.
  refuse_rcpt     addresses (case-insensitive) whose RCPT is answered "550 5.1.1 no such user"; the
                  others are accepted, so smtplib returns a refusals dict (a typo in one list).
"""

from __future__ import annotations

import re
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
                if verb == "EHLO" and srv.size_limit is not None:
                    self._send("250-test-smtp")
                    self._send(f"250 SIZE {srv.size_limit}")
                else:
                    self._send("250 test-smtp")
            elif verb == "MAIL":
                srv.mail_attempts += 1
                if srv.fail_with:
                    self._send(srv.fail_with)
                    continue
                declared = re.search(r"SIZE=(\d+)", cmd, re.I)
                if srv.size_limit and declared and int(declared.group(1)) > srv.size_limit:
                    self._send("552 5.3.4 message size exceeds fixed limit")
                    continue
                mail_from, rcpts = cmd, []
                self._send("250 OK")
            elif verb == "RCPT":
                if any(a.lower() in cmd.lower() for a in srv.refuse_rcpt):
                    self._send("550 5.1.1 no such user")
                    continue
                rcpts.append(cmd)
                self._send("250 OK")
            elif verb == "DATA":
                self._send("354 end with .")
                lines, received, hung_up = [], 0, False
                while True:
                    ln = self.rfile.readline().decode("utf-8", "replace")
                    if ln in (".\r\n", ".\n", ""):
                        break
                    lines.append(ln)
                    received += len(ln)
                    if srv.hangup_over is not None and received > srv.hangup_over:
                        srv.hangups += 1
                        hung_up = True
                        break
                if hung_up:
                    return                      # connection closed mid-DATA, nothing delivered
                if srv.reject_attachments and "Content-Disposition: attachment" in "".join(lines):
                    self._send(srv.reject_attachments)
                    continue
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
def smtp_server(fail_with: str | None = None, *, size_limit: int | None = None,
                reject_attachments: str | None = None, hangup_over: int | None = None,
                refuse_rcpt: tuple[str, ...] = ()):
    srv = _Server(("127.0.0.1", 0), _Handler)
    srv.messages = []
    srv.mail_attempts = 0
    srv.fail_with = fail_with
    srv.size_limit = size_limit
    srv.reject_attachments = reject_attachments
    srv.hangup_over = hangup_over
    srv.refuse_rcpt = tuple(refuse_rcpt)
    srv.hangups = 0
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
