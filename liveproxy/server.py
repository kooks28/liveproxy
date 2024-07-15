import base64
import errno
import logging
import os
import random
import re
import shlex
import socket
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from shutil import which
from socketserver import ThreadingMixIn
from time import time
from urllib.parse import unquote

ACCEPTABLE_ERRNO = (
    errno.ECONNABORTED,
    errno.ECONNRESET,
    errno.EINVAL,
    errno.EPIPE,
)
try:
    ACCEPTABLE_ERRNO += (errno.WSAECONNABORTED,)
except AttributeError:
    pass  # Not windows

_re_ffmpeg = re.compile(r"ffmpeg", re.IGNORECASE)
_re_streamlink = re.compile(r"streamlink", re.IGNORECASE)
_re_youtube_dl = re.compile(r"(?:youtube|yt)[_-]dl(?:p)?", re.IGNORECASE)

log = logging.getLogger(__name__.replace("liveproxy.", ""))

# Global variables to manage the stream process and history
current_process = None
last_played_command = None

class HTTPRequest(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    def _headers(self, status, content, connection=False):
        self.send_response(status)
        self.send_header("Server", "LiveProxy")
        self.send_header("Content-type", content)
        if connection:
            self.send_header("Connection", connection)
        self.end_headers()

    def do_HEAD(self):
        """Respond to a HEAD request."""
        self._headers(404, "text/html", connection="close")

    def do_GET(self):
        """Respond to a GET request."""
        global current_process
        global last_played_command

        random_id = hex(int(time()))[5:]
        log = logging.getLogger("{name}.{random_id}".format(
            name=__name__.replace("liveproxy.", ""),
            random_id=random_id,
        ))

        log.info(f"User-Agent: {self.headers.get('User-Agent', '???')}")
        log.info(f"Client: {self.client_address}")
        log.info(f"Address: {self.address_string()}")

        if self.path.startswith(("/base64/")) or self.path.startswith(("/random-base64/")):
            # http://127.0.0.1:53422/base64/STREAMLINK-COMMANDS[|STREAMLINK-COMMANDS|FFMPEG-COMMANDS|YOUTUBE-DL-COMMANDS|YT-DLP-COMMANDS]/
            # http://127.0.0.1:53422/base64/FFMPEG-COMMANDS[|STREAMLINK-COMMANDS|FFMPEG-COMMANDS|YOUTUBE-DL-COMMANDS|YT-DLP-COMMANDS]/
            # http://127.0.0.1:53422/base64/YOUTUBE-DL-COMMANDS[|STREAMLINK-COMMANDS|FFMPEG-COMMANDS|YOUTUBE-DL-COMMANDS|YT-DLP-COMMANDS]/
            # http://127.0.0.1:53422/base64/YT-DLP-COMMANDS[|STREAMLINK-COMMANDS|FFMPEG-COMMANDS|YOUTUBE-DL-COMMANDS|YT-DLP-COMMANDS]/
            try:
                commands = base64.urlsafe_b64decode(self.path.split("/")[2]).decode("UTF-8").split('|')
            except base64.binascii.Error as err:
                log.error(f"invalid base64 URL: {err}")
                self._headers(404, "text/html", connection="close")
                return
        elif self.path.startswith(("/cmd/")) or self.path.startswith(("/random-cmd/")):
            # http://127.0.0.1:53422/cmd/streamlink https://example best[|streamlink https://example best]/
            # http://127.0.0.1:53422/cmd/streamlink -i https://example[|streamlink -i https://example]/
            self.path = self.path[5:]
            if self.path.endswith("/"):
                self.path = self.path[:-1]
            commands = unquote(self.path).split('|')
        else:
            self._headers(404, "text/html", connection="close")
            return

        # Check if the requested command is the same as the last played one
        if self.path == last_played_command:
            #log.warning("Blocking request for the same stream that was just played.")
            self._headers(403, "text/html", connection="close")  # Return Forbidden (403)
            return

        if self.path.startswith(("/random-")) and len(commands) > 1:
            random.shuffle(commands)

        arglists = [shlex.split(command) for command in commands]

        # Terminate any existing stream before starting a new one
        if current_process is not None:
            try:
                log.info("Terminating previous stream...")
                current_process.terminate()
                current_process.wait()
                current_process.kill()
                log.info("Previous stream terminated.")
            except Exception as e:
                log.error(f"Error terminating previous stream: {e}")

        for arglist in arglists:
            prog = which(arglist[0], mode=os.F_OK | os.X_OK)
            if not prog:
                log.error(f"invalid prog, can not find '{arglist[0]}' on your system")
                continue

            log.debug(f"Video-Software: {prog}")
            if _re_ffmpeg.search(prog):
                arglist.extend(["-hide_banner", "-loglevel", "error", "-nostats", "-nostdin", "-f", "mpegts", "-"])
            elif _re_streamlink.search(prog):
                arglist.extend(["--stdout", "--loglevel", "none"])
            elif _re_youtube_dl.search(prog):
                arglist.extend(["-o", "-", "--quiet", "--no-playlist", "--no-warnings", "--no-progress"])
            else:
                log.error("Video-Software is not supported.")
                self._headers(404, "text/html", connection="close")
                continue

            log.debug(f"{arglist!r}")
            self._headers(200, "video/unknown")
            process = subprocess.Popen(arglist,
                                       stderr=subprocess.PIPE,
                                       stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE,
                                       shell=False,
                                       )

            current_process = process  # Store the new process as the current one
            last_played_command = self.path  # Store the currently playing command

            log.info(f"Stream started {random_id}")
            try:
                 while True:
                    read = process.stdout.readline()
                    if read:
                        self.wfile.write(read)
                    sys.stdout.flush()
                    if process.poll() is not None:
                        self.wfile.close()
                        break
            except socket.error as e:
                if isinstance(e.args, tuple):
                    if not e.errno in ACCEPTABLE_ERRNO:
                        log.error(f"E1: {e!r}")
                else:
                    log.error(f"E2: {e!r}")

            log.info(f"Stream ended {random_id}")
            process.terminate()
            process.wait()
            process.kill()


class Server(HTTPServer):
    """HTTPServer class with timeout."""
    timeout = 5

    def finish_request(self, request, client_address):
        """Finish one request by instantiating RequestHandlerClass."""
        try:
            self.RequestHandlerClass(request, client_address, self)
        except ValueError:
            pass
        except socket.error as err:
            if err.errno not in ACCEPTABLE_ERRNO:
                raise


class ThreadedHTTPServer(ThreadingMixIn, Server):
    """Handle requests in a separate thread."""
    allow_reuse_address = True
    daemon_threads = True


__all__ = ("HTTPRequest", "ThreadedHTTPServer")
