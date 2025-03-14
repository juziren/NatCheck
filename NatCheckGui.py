import os
import sys
import time
import socket
import struct
import codecs
import tkinter as tk
from tkinter import messagebox, font
import threading
import queue

__version__ = "1.0"

def new_socket_reuse(family, type):
    sock = socket.socket(family, type)
    if hasattr(socket, "SO_REUSEADDR"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    return sock
    
class Status(object):
    NA      = 0
    OK      = 1
    COMPAT  = 2
    FAIL    = 3
    @staticmethod
    def rep(status):
        return { 
            Status.NA:      "[   NA   ]",
            Status.OK:      "[   OK   ]",
            Status.COMPAT:  "[ COMPAT ]",
            Status.FAIL:    "[  FAIL  ]"
        }[status]

class StunTest(object):
    stun_server_tcp = [
        "fwa.lifesizecloud.com",
        "global.turn.twilio.com",
        "turn.cloudflare.com",
        "stun.voip.blackberry.com",
        "stun.radiojar.com",
        "stun.isp.net.au"
    ]
    stun_server_udp = [
        "stun.miwifi.com",
        "stun.chat.bilibili.com",
        "stun.hitv.com",
        "stun.cdnbye.com"
    ]
    port_test_server = "portcheck.transmissionbt.com"
    keep_alive_server = "www.baidu.com"
    MTU         = 1500
    STUN_PORT   = 3478
    MAGIC_COOKIE    = 0x2112a442
    BIND_REQUEST    = 0x0001
    BIND_RESPONSE   = 0x0101
    FAMILY_IPV4     = 0x01
    FAMILY_IPV6     = 0x02
    CHANGE_PORT     = 0x0002
    CHANGE_IP       = 0x0004
    ATTRIB_MAPPED_ADDRESS      = 0x0001
    ATTRIB_CHANGE_REQUEST      = 0x0003
    ATTRIB_XOR_MAPPED_ADDRESS  = 0x0020
    NAT_UNKNOWN          = -1
    NAT_OPEN_INTERNET    = 0
    NAT_FULL_CONE        = 1
    NAT_RESTRICTED       = 2
    NAT_PORT_RESTRICTED  = 3
    NAT_SYMMETRIC        = 4
    NAT_SYM_UDP_FIREWALL = 5

    def init(self, source_ip = "0.0.0.0"):
        self.source_ip = source_ip
        self.stun_ip_tcp = []
        self.stun_ip_udp = []
        for hostname in self.stun_server_tcp:
            self.stun_ip_tcp.extend(self._resolve_hostname(hostname))
        for hostname in self.stun_server_udp:
            self.stun_ip_udp.extend(self._resolve_hostname(hostname))
        if not self.stun_ip_tcp or not self.stun_ip_udp:
            raise RuntimeError("无法解析主机名")

    def _get_free_port(self, udp=False):
        socket_type = socket.SOCK_DGRAM if udp else socket.SOCK_STREAM
        sock = new_socket_reuse(socket.AF_INET, socket_type)
        sock.bind(("", 0))
        ret = sock.getsockname()[1]
        sock.close()
        return ret

    def _resolve_hostname(self, hostname):
        try:
            host, alias, ip_addresses = socket.gethostbyname_ex(hostname)
            return ip_addresses
        except (socket.error, OSError) as e:
            return []

    def _random_tran_id(self, use_magic_cookie = False):
        if use_magic_cookie:
            return struct.pack("!L", self.MAGIC_COOKIE) + os.urandom(12)
        else:
            return os.urandom(16)

    def _pack_stun_message(self, msg_type, tran_id, payload = b""):
        return struct.pack("!HH", msg_type, len(payload)) + tran_id + payload

    def _unpack_stun_message(self, data):
        msg_type, msg_length = struct.unpack("!HH", data[:4])
        tran_id = data[4:20]
        payload = data[20:20 + msg_length]
        return msg_type, tran_id, payload

    def _extract_mapped_addr(self, payload):
        while payload:
            attrib_type, attrib_length = struct.unpack("!HH", payload[:4])
            attrib_value = payload[4:4 + attrib_length]
            payload = payload[4 + attrib_length:]
            if attrib_type == self.ATTRIB_MAPPED_ADDRESS:
                _, family, port = struct.unpack("!BBH", attrib_value[:4])
                if family == self.FAMILY_IPV4:
                    ip = socket.inet_ntoa(attrib_value[4:8])
                    return ip, port
            elif attrib_type == self.ATTRIB_XOR_MAPPED_ADDRESS:
                _, family, xor_port = struct.unpack("!BBH", attrib_value[:4])
                if family == self.FAMILY_IPV4:
                    xor_iip, = struct.unpack("!L", attrib_value[4:8])
                    ip = socket.inet_ntoa(struct.pack("!L", self.MAGIC_COOKIE ^ xor_iip))
                    port = (self.MAGIC_COOKIE >> 16) ^ xor_port
                    return ip, port
        return None

    def tcp_test(self, stun_host, source_port, timeout = 3):
        tran_id = self._random_tran_id(use_magic_cookie = True)
        sock = new_socket_reuse(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(timeout)
        try:
            sock.bind((self.source_ip, source_port))
            sock.connect((stun_host, self.STUN_PORT))
            data = self._pack_stun_message(self.BIND_REQUEST, tran_id)
            sock.sendall(data)
            buf = sock.recv(self.MTU)
            msg_type, msg_id, payload = self._unpack_stun_message(buf)
            if tran_id == msg_id and msg_type == self.BIND_RESPONSE:
                source_addr  = sock.getsockname()
                mapped_addr = self._extract_mapped_addr(payload)
                ret = source_addr, mapped_addr
            else:
                ret = None
            sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        except Exception as e:
            sock.close()
            ret = None
        return ret

    def udp_test(self, stun_host, source_port, change_ip = False, change_port = False, timeout = 3, repeat = 3):
        time_start = time.time()
        tran_id = self._random_tran_id()
        sock = new_socket_reuse(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            sock.bind((self.source_ip, source_port))
            flags = 0
            if change_ip:
                flags |= self.CHANGE_IP
            if change_port:
                flags |= self.CHANGE_PORT
            if flags:
                payload = struct.pack("!HHL", self.ATTRIB_CHANGE_REQUEST, 0x4, flags)
                data = self._pack_stun_message(self.BIND_REQUEST, tran_id, payload)
            else:
                data = self._pack_stun_message(self.BIND_REQUEST, tran_id)
            for _ in range(repeat):
                sock.sendto(data, (stun_host, self.STUN_PORT))
            while True:
                time_left = time_start + timeout - time.time()
                if time_left <= 0:
                    return None
                sock.settimeout(time_left)
                buf, recv_addr = sock.recvfrom(self.MTU)
                recv_host, recv_port = recv_addr
                if len(buf) < 20:
                    continue
                msg_type, msg_id, payload = self._unpack_stun_message(buf)
                if tran_id!= msg_id or msg_type!= self.BIND_RESPONSE:
                    continue
                source_addr  = sock.getsockname()
                mapped_addr  = self._extract_mapped_addr(payload)
                ip_changed   = (recv_host!= stun_host)
                port_changed = (recv_port!= self.STUN_PORT)
                return source_addr, mapped_addr, ip_changed, port_changed
        except Exception:
            return None
        finally:
            sock.close()

    def get_tcp_mapping(self, source_port = 0):
        server_ip = first = self.stun_ip_tcp[0]
        while True:
            ret = self.tcp_test(server_ip, source_port)
            if ret is None:
                self.stun_ip_tcp.append(self.stun_ip_tcp.pop(0))
                server_ip = self.stun_ip_tcp[0]
                if server_ip == first:
                    raise RuntimeError("无可用 STUN 服务器")
            else:
                source_addr, mapped_addr = ret
                return source_addr, mapped_addr

    def get_udp_mapping(self, source_port = 0):
        server_ip = first = self.stun_ip_udp[0]
        while True:
            ret = self.udp_test(server_ip, source_port)
            if ret is None:
                self.stun_ip_udp.append(self.stun_ip_udp.pop(0))
                server_ip = self.stun_ip_udp[0]
                if server_ip == first:
                    raise RuntimeError("无可用 STUN 服务器")
            else:
                source_addr, mapped_addr, ip_changed, port_changed = ret
                return source_addr, mapped_addr

    def _check_tcp_cone(self, source_port = 0):
        if source_port == 0:
            source_port = self._get_free_port()
        mapped_addr_first = None
        count = 0
        for server_ip in self.stun_ip_tcp:
            if count >= 3:
                return 1
            ret = self.tcp_test(server_ip, source_port)
            if ret is not None:
                source_addr, mapped_addr = ret
                if mapped_addr_first is not None and mapped_addr!= mapped_addr_first:
                    return -1
                mapped_addr_first = ret[1]
                count += 1
        return 0

    def _check_tcp_fullcone(self, source_port = 0):
        if source_port == 0:
            source_port = self._get_free_port()
        srv_sock = new_socket_reuse(socket.AF_INET, socket.SOCK_STREAM)
        try:
            srv_sock.bind((self.source_ip, source_port))
            srv_sock.listen(5)
        except (OSError, socket.error):
            srv_sock.close()
            return 0
        ka_sock = new_socket_reuse(socket.AF_INET, socket.SOCK_STREAM)
        try:
            ka_sock.bind((self.source_ip, source_port))
            ka_sock.connect((self.keep_alive_server, 80))
            ka_sock.sendall((
                "GET /~ HTTP/1.1\r\nHost: %s\r\nConnection: keep-alive\r\n\r\n" % self.keep_alive_server
            ).encode())
            source_addr, mapped_addr = self.get_tcp_mapping(source_port)
            public_port = mapped_addr[1]
        except (OSError, socket.error):
            srv_sock.close()
            ka_sock.close()
            return 0
        if source_addr == mapped_addr:
            return 2
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(8)
        try:
            sock.bind((self.source_ip, 0))
            sock.connect((StunTest.port_test_server, 80))
            sock.sendall((
                "GET /%d HTTP/1.0\r\n"
                "Host: %s\r\n"
                "User-Agent: curl/8.0.0 (Nat)\r\n"
                "Accept: /\r\n"
                "Connection: close\r\n"
                "\r\n" % (public_port, StunTest.port_test_server)
            ).encode())
            response = b""
            while True:
                buff = sock.recv(4096)
                if not buff:
                    break
                response += buff
            _, content = response.split(b"\r\n\r\n", 1)
            if content.strip() == b"1":
                return 1
            elif content.strip() == b"0":
                return -1
            raise ValueError("意外响应: %s" % response)
        except (OSError, LookupError, ValueError, TypeError, socket.error) as ex:
            return 0
        finally:
            ka_sock.close()
            srv_sock.close()
            sock.close()

    def check_udp_nat_type(self, source_port = 0):
        ret_test1_1 = None
        ret_test1_2 = None
        ret_test2 = None
        ret_test3 = None
        if source_port == 0:
            source_port = self._get_free_port(udp=True)
        for server_ip in self.stun_ip_udp:
            ret = self.udp_test(server_ip, source_port, change_ip=False, change_port=False)
            if ret is None:
                continue
            if ret_test1_1 is None:
                ret_test1_1 = ret
                continue
            ret_test1_2 = ret
            ret = self.udp_test(server_ip, source_port, change_ip=True, change_port=True)
            if ret is not None:
                source_addr, mapped_addr, ip_changed, port_changed = ret
                if not ip_changed or not port_changed:
                    continue
            ret_test2 = ret
            ret_test3 = self.udp_test(server_ip, source_port, change_ip=False, change_port=True)
            break
        else:
            return StunTest.NAT_UNKNOWN
        source_addr_1_1, mapped_addr_1_1, _, _ = ret_test1_1
        source_addr_1_2, mapped_addr_1_2, _, _ = ret_test1_2
        if mapped_addr_1_1!= mapped_addr_1_2:
            return StunTest.NAT_SYMMETRIC
        if source_addr_1_1 == mapped_addr_1_1:
            if ret_test2 is not None:
                return StunTest.NAT_OPEN_INTERNET
            else:
                return StunTest.NAT_SYM_UDP_FIREWALL
        else:
            if ret_test2 is not None:
                return StunTest.NAT_FULL_CONE
            else:
                if ret_test3 is not None:
                    return StunTest.NAT_RESTRICTED
                else:
                    return StunTest.NAT_PORT_RESTRICTED

    def check_tcp_nat_type(self, source_port = 0):
        if source_port == 0:
            source_port = self._get_free_port()
        ret = self._check_tcp_fullcone(source_port)
        if ret == 2:
            return StunTest.NAT_OPEN_INTERNET
        elif ret == 1:
            return StunTest.NAT_FULL_CONE
        elif ret == 0:
            return StunTest.NAT_UNKNOWN
        ret = self._check_tcp_cone()
        if ret == 1:
            return StunTest.NAT_PORT_RESTRICTED
        elif ret == -1:
            return StunTest.NAT_SYMMETRIC
        else:
            return StunTest.NAT_UNKNOWN

class Check(object):
    def __init__(self):  # 这是修正后的初始化方法
        self.stun_test = StunTest()
        try:
            self.stun_test.init()
        except Exception as e:
            raise RuntimeError(f"STUN测试初始化失败: {str(e)}")

    def do_check(self, result_queue):
        try:
            status, info = self._check_tcp_nat()
            result_queue.put((f"检查TCP NAT.", status, info))
            status, info = self._check_udp_nat()
            result_queue.put((f"检查UDP NAT.", status, info))
            result_queue.put(None)  # Signal completion
        except Exception as e:
            result_queue.put(("错误", Status.FAIL, str(e)))
            result_queue.put(None)  # Signal completion

    def _check_tcp_nat(self):
        type = self.stun_test.check_tcp_nat_type()
        info = "NAT 类型: %s" % [
            "未知", "开放网络", "全锥型", "受限锥型", "端口受限锥型", "对称型", "UDP防火墙"
        ][type + 1]
        status = Status.OK if type in [0, 1] else Status.FAIL
        return status, info

    def _check_udp_nat(self):
        type = self.stun_test.check_udp_nat_type()
        info = "NAT 类型: %s" % [
            "未知", "开放网络", "全锥型", "受限锥型", "端口受限锥型", "对称型", "UDP防火墙"
        ][type + 1]
        status = Status.OK if type in [0, 1] else Status.FAIL
        return status, info


def main():
    root = tk.Tk()
    root.title(f"Nat类型检测 v{__version__}")
    #root.geometry("500x300")
    root.geometry("500x300+600+300")

    # 设置字体
    custom_font = font.Font(family="Helvetica", size=12)

    # 创建并配置结果文本框
    result_text = tk.Text(root, height=15, width=70, wrap=tk.WORD, font=custom_font)
    result_text.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")

    # 创建并配置开始按钮
    start_button = tk.Button(root, text="开始检查", command=lambda: run_checks(result_text, start_button), font=custom_font)
    start_button.grid(row=1, column=0, pady=10)

    # 配置网格布局权重
    root.grid_rowconfigure(0, weight=1)
    root.grid_columnconfigure(0, weight=1)

    def update_result(text, status, info):
        result_text.insert(tk.END, f"{text:<30} {Status.rep(status)} {info}\n")
        result_text.see(tk.END)
        result_text.update_idletasks()

    def run_checks(result_text, start_button):
        start_button.config(state=tk.DISABLED)  # 禁用按钮

        result_queue = queue.Queue()

        def process_queue():
            try:
                while True:
                    item = result_queue.get_nowait()
                    if item is None:
                        break
                    text, status, info = item
                    update_result(text, status, info)
            except queue.Empty:
                root.after(100, process_queue)
            else:
                start_button.config(state=tk.NORMAL)  # 检查完成后重新启用按钮

        def worker():
            check = Check()
            check.do_check(result_queue)
        
        thread = threading.Thread(target=worker)
        thread.start()
        process_queue()

    root.mainloop()


if __name__ == "__main__":
    main()