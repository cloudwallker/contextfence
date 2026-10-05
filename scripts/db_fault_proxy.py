#!/usr/bin/env python3
"""Project-private PostgreSQL TCP fault proxy; control listens inside its own container only."""
import argparse
import http.server
import json
import os
import socket
import socketserver
import threading


def close_socket(connection):
    try:connection.shutdown(socket.SHUT_RDWR)
    except OSError:pass
    try:connection.close()
    except OSError:pass


class TcpFaultProxy:
    def __init__(self,listen,upstream):
        self.upstream=upstream;self.lock=threading.RLock();self.blocked=False;self.connections=set();owner=self
        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address=True
            daemon_threads=True
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):owner.forward(self.request)
        self.server=Server(listen,Handler);self.address=self.server.server_address
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
    def forward(self,client):
        with self.lock:
            if self.blocked:close_socket(client);return
        try:upstream=socket.create_connection(self.upstream,timeout=3);upstream.settimeout(None)
        except OSError:close_socket(client);return
        with self.lock:
            # A cut during upstream connect must also deny this in-flight connection.
            if self.blocked:close_socket(client);close_socket(upstream);return
            self.connections.update((client,upstream))
        def relay(source,target):
            try:
                while True:
                    data=source.recv(65536)
                    if not data:return
                    target.sendall(data)
            except OSError:pass
            finally:close_socket(source);close_socket(target)
        outbound=threading.Thread(target=relay,args=(client,upstream),daemon=True);outbound.start()
        relay(upstream,client);outbound.join(timeout=5)
        with self.lock:self.connections.discard(client);self.connections.discard(upstream)
    def cut(self):
        with self.lock:
            self.blocked=True
            count=len(self.connections)//2
            for connection in list(self.connections):close_socket(connection)
            self.connections.clear()
            return count
    def restore(self):
        with self.lock:self.blocked=False
    def state(self):
        with self.lock:return {'blocked':self.blocked,'forwarded_connections':len(self.connections)//2}
    def __enter__(self):self.thread.start();return self
    def __exit__(self,*args):self.cut();self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target-host',default=os.environ.get('DB_FAULT_TARGET_HOST','postgres'))
    parser.add_argument('--target-port',type=int,default=5432);parser.add_argument('--listen-port',type=int,default=5432)
    parser.add_argument('--control-port',type=int,default=18888);args=parser.parse_args()
    with TcpFaultProxy(('0.0.0.0',args.listen_port),(args.target_host,args.target_port)) as proxy:
        class Control(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def reply(self,status,document):
                data=json.dumps(document).encode();self.send_response(status)
                self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)))
                self.end_headers();self.wfile.write(data)
            def do_GET(self):self.reply(200 if self.path=='/health' else 404,proxy.state())
            def do_POST(self):
                if self.path=='/cut':self.reply(200,{'closed_connections':proxy.cut(),**proxy.state()})
                elif self.path=='/restore':proxy.restore();self.reply(200,proxy.state())
                else:self.reply(404,{'code':'NOT_FOUND'})
        # The control endpoint has no container-network or host listener; use project-scoped docker exec.
        http.server.ThreadingHTTPServer(('127.0.0.1',args.control_port),Control).serve_forever()


if __name__=='__main__':main()
