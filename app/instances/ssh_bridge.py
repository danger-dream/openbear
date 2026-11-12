"""ProxyCommand for SSH to a front: joins ssh's stdin and stdout to the controller's unix socket,
behind which the front's exit holds the TCP connection. Run as: python -I ssh_bridge.py SOCKET."""
import os
import socket
import sys
import threading


def main(path):
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.connect(path)

    def upstream():
        try:
            while data := os.read(0, 65536):
                conn.sendall(data)
        except OSError:
            pass
        finally:
            try:
                conn.shutdown(socket.SHUT_WR)
            except OSError:
                pass
    threading.Thread(target=upstream, daemon=True).start()
    try:
        while data := conn.recv(65536):
            # A pipe may take part of it.
            view = memoryview(data)
            while view:
                view = view[os.write(1, view):]
    except OSError:
        pass


if __name__ == '__main__':
    main(sys.argv[1])
