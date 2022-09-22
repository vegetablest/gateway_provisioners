import base64
import json
import logging
import os
import random
import signal
import socket
import uuid
from multiprocessing import Process
from threading import Thread
from typing import Dict, List, Optional

from Cryptodome.Cipher import AES, PKCS1_v1_5
from Cryptodome.PublicKey import RSA
from Cryptodome.Random import get_random_bytes
from Cryptodome.Util.Padding import pad
from jupyter_client.connect import write_connection_file

LAUNCHER_VERSION = 1  # Indicate to server the version of this launcher (payloads may vary)

max_port_range_retries = int(os.getenv("MAX_PORT_RANGE_RETRIES", "5"))

log_level = os.getenv("LOG_LEVEL", "10")
log_level = int(log_level) if log_level.isdigit() else log_level

logging.basicConfig(format="[%(levelname)1.1s %(asctime)s.%(msecs).03d %(name)s] %(message)s")

logger = logging.getLogger("server_listener")
logger.setLevel(log_level)


class ServerListener:
    def __init__(self,
                 conn_filename: str,
                 parent_pid: int,
                 lower_port: int,
                 upper_port: int,
                 response_addr: str,
                 kernel_id: str,
                 public_key: str,
                 cluster_type: Optional[str] = None,
                 as_thread: Optional[bool] = True):
        # Note, in the R integration, parameters come into Python as strings, so
        # we need to explicitly cast non-strings.
        self.conn_filename: str = conn_filename
        self.parent_pid: int = int(parent_pid)
        self.lower_port: int = int(lower_port)
        self.upper_port: int = int(upper_port)
        self.response_addr: str = response_addr
        self.kernel_id: str = kernel_id
        self.public_key: bytes = public_key.encode("utf-8")
        self.cluster_type: str = cluster_type
        self.as_thread: bool = bool(as_thread)
        # Initialized later...
        self.comm_socket: Optional[socket] = None

    def build_connection_file(self) -> None:

        ports: List = self._select_ports(5)
        write_connection_file(
            fname=self.conn_filename,
            ip="0.0.0.0",
            key=str(uuid.uuid4()).encode("utf-8"),  # convert to bytes,
            shell_port=ports[0],
            iopub_port=ports[1],
            stdin_port=ports[2],
            hb_port=ports[3],
            control_port=ports[4],
        )

    def _encrypt(self, connection_info_bytes: bytes) -> bytes:
        """Encrypt the connection information using a generated AES key that is then encrypted using
        the public key passed from the server.  Both are then returned in an encoded JSON payload.
        """
        aes_key = get_random_bytes(16)
        cipher = AES.new(aes_key, mode=AES.MODE_ECB)

        # Encrypt the connection info using the aes_key
        encrypted_connection_info = cipher.encrypt(pad(connection_info_bytes, 16))
        b64_connection_info = base64.b64encode(encrypted_connection_info)

        # Encrypt the aes_key using the server's public key
        imported_public_key = RSA.importKey(base64.b64decode(self.public_key))
        cipher = PKCS1_v1_5.new(key=imported_public_key)
        encrypted_key = base64.b64encode(cipher.encrypt(aes_key))

        # Compose the payload and Base64 encode it
        payload = {
            "version": LAUNCHER_VERSION,
            "key": encrypted_key.decode(),
            "conn_info": b64_connection_info.decode(),
        }
        b64_payload = base64.b64encode(json.dumps(payload).encode(encoding="utf-8"))
        return b64_payload

    def return_connection_info(self) -> None:
        """Returns the connection information corresponding to this kernel."""
        response_parts = self.response_addr.split(":")
        if len(response_parts) != 2:
            logger.error(
                f"Invalid format for response address '{self.response_addr}'. Assuming 'pull' mode..."
            )
            return

        response_ip = response_parts[0]
        try:
            response_port = int(response_parts[1])
        except ValueError:
            logger.error(
                f"Invalid port component found in response address '{self.response_addr}'. Assuming 'pull' mode..."
            )
            return

        with open(self.conn_filename) as fp:
            cf_json = json.load(fp)
            fp.close()

        # add process and process group ids into connection info
        cf_json["pid"] = self.parent_pid
        cf_json["pgid"] = os.getpgid(self.parent_pid)

        # prepare socket address for handling signals
        self.prepare_comm_socket()  # self.comm_socket initialized
        cf_json["comm_port"] = self.comm_socket.getsockname()[1]
        cf_json["kernel_id"] = self.kernel_id

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((response_ip, response_port))
            json_content = json.dumps(cf_json).encode(encoding="utf-8")
            logger.debug(f"JSON Payload '{json_content}")
            payload = self._encrypt(json_content)
            logger.debug(f"Encrypted Payload '{payload}")
            s.send(payload)

    def prepare_comm_socket(self) -> None:
        """Prepares the socket to which the server will send signal and shutdown requests."""
        self.comm_socket = self._select_socket()
        logger.info(
            f"Signal socket bound to host: "
            f"{self.comm_socket.getsockname()[0]}, port: {self.comm_socket.getsockname()[1]}"
        )
        self.comm_socket.listen(1)
        self.comm_socket.settimeout(5)

    def _select_ports(self, count: int) -> List:
        """Select and return n random ports that are available and adhere to the given port range, if applicable."""
        ports = []
        sockets = []
        for _ in range(count):
            sock = self._select_socket()
            ports.append(sock.getsockname()[1])
            sockets.append(sock)
        for sock in sockets:
            sock.close()
        return ports

    def _select_socket(self) -> socket:
        """Create and return a socket whose port is available and adheres to the given port range, if applicable."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        found_port = False
        retries = 0
        while not found_port:
            try:
                sock.bind(("0.0.0.0", self._get_candidate_port()))
                found_port = True
            except OSError:
                retries = retries + 1
                if retries > max_port_range_retries:
                    raise RuntimeError(
                        f"Failed to locate port within range {self.lower_port}..{self.upper_port} "
                        f"after {max_port_range_retries} retries!"
                    )
        return sock

    def _get_candidate_port(self) -> int:
        """Returns a port within the given range.  If the range is zero, the zero is returned."""
        range_size = self.upper_port - self.lower_port
        if range_size == 0:
            return 0
        return random.randint(self.lower_port, self.upper_port)

    def get_server_request(self) -> Dict:
        """Gets a request from the server and returns the corresponding dictionary."""
        conn: socket = None
        data: str = ""
        request_info: Optional[Dict] = None
        try:
            logger.info("DEBUG: get_server_request: waiting for socket accept")
            conn, addr = self.comm_socket.accept()
            logger.info("DEBUG: get_server_request: socket accepted")
            while True:
                buffer: bytes = conn.recv(1024)
                if buffer == b'':  # send is complete
                    if len(data) > 0:
                        request_info = json.loads(data)
                    else:
                        logger.info("DEBUG: get_server_request: no data received - returning None")
                    break
                else:
                    logger.info(f"DEBUG: get_server_request: received buffer: '{buffer}'")
                data = data + buffer.decode("utf-8")  # append what we received until we get no more...
        except Exception as ex:
            if type(ex) is not socket.timeout:
                raise ex
        finally:
            if conn:
                conn.close()

        return request_info

    def process_requests(self) -> None:
        """Waits for requests from the server and processes each when received.  Currently,
        these will be one of a sending a signal to the corresponding kernel process (signum) or
        stopping the listener and exiting the kernel (shutdown).
        """

        # Since this creates the communication socket, we should do this here so the socket
        # gets created in the sub-process/thread.  This is necessary on MacOS/Python.
        self.return_connection_info()

        shutdown = False
        while not shutdown:
            request = self.get_server_request()
            if request:
                signum = -1  # prevent logging poll requests since that occurs every 3 seconds
                if request.get("signum") is not None:
                    signum = int(request.get("signum"))
                    os.kill(self.parent_pid, signum)
                    if signum == 2 and self.cluster_type == "spark":
                        os.kill(self.parent_pid, signal.SIGUSR2)
                if request.get("shutdown") is not None:
                    shutdown = bool(request.get("shutdown"))
                if signum != 0:
                    logger.info(f"server_listener got request: {request}")


def setup_server_listener(
        conn_filename: str,
        parent_pid: int,
        lower_port: int,
        upper_port: int,
        response_addr: str,
        kernel_id: str,
        public_key: str,
        cluster_type: Optional[str] = None,
        as_thread: Optional[bool] = True
) -> None:
    """Initializes the server listener thread or process depending on the `as_thread` parameter.

    Currently, R kernels use a thread for the listener while Python kernels use a process.
    """

    # Create the ServerListener instance and build the connection file PRIOR to sub-process.
    # This is synchronous relative to the launcher, so the launcher can start the kernel
    # using the connection file and no race condition is introduced.
    sl = ServerListener(conn_filename, parent_pid, lower_port, upper_port, response_addr,
                        kernel_id, public_key, cluster_type, as_thread)
    sl.build_connection_file()

    server_listener = Thread(target=sl.process_requests) if as_thread else Process(target=sl.process_requests)
    server_listener.start()


__all__ = [
    "setup_server_listener",
]
