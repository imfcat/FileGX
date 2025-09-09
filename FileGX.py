import os
import json
import shutil
import mimetypes
import socket
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import ttk, messagebox, filedialog
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import urllib.parse
import traceback

CONFIG_FILE = 'config.json'

def load_config():
    default = {'port': 8080, 'shared_files': []}
    if os.path.exists(CONFIG_FILE):
        default.update(json.load(open(CONFIG_FILE, encoding='utf-8')))
    return default

def save_config(cfg):
    json.dump(cfg, open(CONFIG_FILE, 'w', encoding='utf-8'), indent=2)

def get_host_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip


# HTTP服务
clients = {}
logs = []
_lock = threading.Lock()

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        ip = self.client_address[0]
        ua = self.headers.get('User-Agent', '')
        with _lock:
            if ip not in clients:
                clients[ip] = {"ip": ip, "ua": ua, "since": self.date_time_string()}
                logs.append({"t": time.strftime('%H:%M:%S'), "ip": ip, "event": "connected"})
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == '/':
                self.send_html()
            elif path == '/api/files':
                self.send_json(list(self.server.shared_files.values()))
            elif path == '/api/clients':
                self.send_json(list(clients.values()))
            elif path == '/api/logs':
                self.send_json(logs[-200:])
            elif path == '/download':
                name = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('name', [''])[0]
                self.handle_download(name)
            else:
                self.send_error(404)
        except Exception as e:
            traceback.print_exc()
            self.send_error(500)

    def send_html(self):
        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>FileGX - LAN Share</title>
<style>
body{{font-family:Arial;margin:40px;background:#fafafa}}
table{{border-collapse:collapse;width:100%}}
th,td{{padding:8px 12px;border:1px solid #ddd;text-align:left}}
th{{background:#f5f5f5}}
a{{text-decoration:none;color:#2196F3}}
a:hover{{text-decoration:underline}}
</style>
</head><body>
<h2>Shared Files</h2>
<table><thead><tr><th>Name</th><th>Size</th><th>Download</th></tr></thead>
<tbody id="tb"></tbody></table>
<script>
async function load(){{
  const res = await fetch('/api/files');
  const arr = await res.json();
  const tb = document.getElementById('tb');
  tb.innerHTML = arr.map(f=>`<tr><td>${{f.name}}</td><td>${{f.size}} bytes</td>
    <td><a href="/download?name=${{encodeURIComponent(f.name)}}">download</a></td></tr>`).join('');
}}
load();setInterval(load,2000);
</script></body></html>"""
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode())

    def send_json(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(data)

    def handle_download(self, name):
        # 修复中文错误
        from urllib.parse import quote

        with _lock:
            if name not in self.server.shared_files:
                self.send_error(404)
                return
            fpath = self.server.shared_files[name]["path"]
            if not os.path.exists(fpath):
                self.send_error(404, "File not found")
                return
            size = self.server.shared_files[name]["size"]

        utf8_name = quote(name.encode('utf-8'))
        head = f"attachment; filename*=utf-8''{utf8_name}"

        import email.header
        fallback = email.header.Header(name, 'utf-8').encode()
        head += f'; filename="{fallback}"'

        with open(fpath, 'rb') as f:
            self.send_response(200)
            self.send_header('Content-Type', mimetypes.guess_type(fpath)[0] or 'application/octet-stream')
            self.send_header('Content-Length', str(size))
            self.send_header('Content-Disposition', head)
            self.end_headers()
            shutil.copyfileobj(f, self.wfile)

        logs.append({"t": time.strftime('%H:%M:%S'),
                     "ip": self.client_address[0],
                     "event": f"downloaded {name}"})

    def log_message(self, fmt, *args):
        pass


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.shared_files = {}


def http_server_thread(cfg, shared_files):
    srv = ThreadedHTTPServer(('0.0.0.0', cfg['port']), Handler)
    srv.shared_files = shared_files
    print(f'[+] HTTP server running at http://{get_host_ip()}:{cfg["port"]}')
    srv.serve_forever()


# gui
class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.shared_files = {}
        self.server_thread = None
        self.sync_shared_files()
        self.start_server()


        root.title('FileGX - 本机文件分享 (LAN Share)')
        root.geometry('900x600')
        root.minsize(700, 500)
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('TLabelframe.Label', foreground='#2196F3', font=('微软雅黑', 10, 'bold'))

        # 顶栏
        top = ttk.Frame(root)
        top.pack(fill=tk.X, padx=5, pady=5)
        ttk.Label(top, text='端口：').pack(side=tk.LEFT)
        self.port_var = tk.IntVar(value=self.cfg['port'])
        ttk.Spinbox(top, from_=1024, to=65535, textvariable=self.port_var, width=6).pack(side=tk.LEFT)
        ttk.Button(top, text='应用', command=self.restart_server).pack(side=tk.LEFT, padx=5)
        self.ip_url_var = tk.StringVar()
        ttk.Label(top, text='访问地址：').pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.ip_url_var, state='readonly', width=40).pack(side=tk.LEFT, padx=5)
        ttk.Button(top, text='一键复制', command=self.copy_url).pack(side=tk.LEFT)
        ttk.Button(top, text='打开浏览器', command=self.open_browser).pack(side=tk.LEFT, padx=5)
        ttk.Button(top, text='关于', command=self.show_info).pack(side=tk.RIGHT, padx=5)

        paned = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, pady=5)
        left = ttk.Frame(paned)
        right = ttk.Frame(paned)
        paned.add(left, weight=2)
        paned.add(right, weight=1)

        # 左侧-列表、日志
        ttk.Label(left, text='共享文件（拖拽至此处）').pack(anchor=tk.W, padx=5)
        self.file_tree = ttk.Treeview(left, columns=('size',), height=12, show='tree headings')
        self.file_tree.heading('#0', text='文件名')
        self.file_tree.heading('size', text='大小')
        self.file_tree.column('#0', width=300)
        self.file_tree.column('size', width=100)
        self.file_tree.pack(fill=tk.BOTH, expand=True, padx=5)
        self.file_tree.bind('<Button-3>', self.on_right_click)

        ttk.Label(left, text='传输日志').pack(anchor=tk.W, padx=5, pady=(10, 0))
        self.log_text = tk.Text(left, height=8, state=tk.DISABLED, bg='#f5f5f5')
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 右侧-在线用户
        ttk.Label(right, text='在线用户').pack(anchor=tk.W, padx=5)
        self.user_tree = ttk.Treeview(right, columns=('ua',), height=15, show='headings')
        self.user_tree.heading('#0', text='IP')
        self.user_tree.heading('ua', text='User-Agent')
        self.user_tree.column('#0', width=150)
        self.user_tree.column('ua', width=300)
        self.user_tree.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 拖拽
        self.setup_drop()

        # 刷新
        self.refresh_all()
        self.async_loop(2, self.refresh_all)

    def start_server(self):
        if self.server_thread and self.server_thread.is_alive():
            return
        self.server_thread = threading.Thread(target=http_server_thread, args=(self.cfg, self.shared_files), daemon=True)
        self.server_thread.start()

    def async_loop(self, sec, func):
        def _loop():
            while True:
                try:
                    func()
                except Exception as e:
                    print('[async_loop]', e)
                time.sleep(sec)

        t = threading.Thread(target=_loop, daemon=True)
        t.start()

    def restart_server(self):
        self.cfg['port'] = self.port_var.get()
        save_config(self.cfg)
        messagebox.showinfo('提示', '配置已保存，重启程序后生效')

    def setup_drop(self):
        try:
            from tkinterdnd2 import DND_FILES
            self.file_tree.drop_target_register(DND_FILES)
            self.file_tree.dnd_bind('<<Drop>>', self.on_drop_files)
        except ImportError:
            messagebox.showwarning('提示', '未安装 tkinterdnd2，无法拖拽文件\n pip install tkinterdnd2')

    def on_drop_files(self, event):
        files = root.tk.splitlist(event.data)
        for f in files:
            if os.path.isfile(f):
                self.add_share_file(f)

    def add_share_file(self, file_path):
        fname = os.path.basename(file_path)
        if fname in self.shared_files:
            messagebox.showwarning('已存在', f'{fname} 已在共享列表')
            return
        self.shared_files[fname] = {"name": fname, "path": file_path, "size": os.path.getsize(file_path)}
        self.cfg.setdefault('shared_files', []).append(file_path)
        save_config(self.cfg)
        self.refresh_file_tree()

    def on_right_click(self, event):
        item = self.file_tree.identify_row(event.y)
        if item:
            self.file_tree.selection_set(item)
            menu = tk.Menu(root, tearoff=0)
            menu.add_command(label='移除分享', command=self.remove_selected)
            menu.post(event.x_root, event.y_root)

    def remove_selected(self):
        sel = self.file_tree.selection()
        if sel:
            fname = self.file_tree.item(sel[0])['text']
            self.shared_files.pop(fname, None)
            self.cfg['shared_files'] = [p for p in self.cfg.get('shared_files', []) if os.path.basename(p) != fname]
            save_config(self.cfg)
            self.refresh_file_tree()
            messagebox.showinfo('成功', f'已移除分享：{fname}')

    # 文件检查
    def sync_shared_files(self):
        paths = self.cfg.get('shared_files', [])
        valid = []
        for p in paths:
            if os.path.isfile(p):
                fname = os.path.basename(p)
                self.shared_files[fname] = {"name": fname, "path": p, "size": os.path.getsize(p)}
                valid.append(p)
        self.cfg['shared_files'] = valid
        save_config(self.cfg)

    # 刷新
    def refresh_file_tree(self):
        self.file_tree.delete(*self.file_tree.get_children())
        for f in self.shared_files.values():
            self.file_tree.insert('', tk.END, text=f['name'], values=(f['size'],))

    def refresh_all(self):
        # 文件
        self.refresh_file_tree()
        # 用户
        try:
            users = requests.get(f'http://127.0.0.1:{self.cfg["port"]}/api/clients', timeout=2).json()
        except:
            users = []
        self.user_tree.delete(*self.user_tree.get_children())
        for u in users:
            self.user_tree.insert('', tk.END, values=(u['ip'], u['ua'][:40]))
        # 日志
        try:
            logs = requests.get(f'http://127.0.0.1:{self.cfg["port"]}/api/logs', timeout=2).json()
        except:
            logs = []
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        for lg in logs[-50:]:
            self.log_text.insert(tk.END, f"{lg['t']}  {lg['ip']}  {lg['event']}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)


    def copy_url(self):
        copy_to_clipboard(self.ip_url_var.get())

    def update_ip_url(self):
        ip = get_host_ip()
        port = self.cfg['port']
        self.ip_url_var.set(f'http://{ip}:{port}')

    def open_browser(self):
        webbrowser.open(self.ip_url_var.get())


    def show_info(self):
        info_window = tk.Toplevel(root)
        info_window.title("信息")
        info_window.geometry("300x200")
        info_window.resizable(False, False)

        info_window.transient(root)
        info_window.grab_set()

        ttk.Label(info_window, text="FileGX - 局域网内文件共享工具", background="#f0f0f0").pack(side=tk.TOP, pady=10)
        ttk.Label(info_window, text="版本: 1.0", background="#f0f0f0").pack(side=tk.TOP)
        ttk.Label(info_window, text="协议: MIT", background="#f0f0f0").pack(side=tk.TOP)
        ttk.Label(info_window, text="https://github.com/imfcat/FileGX", background="#f0f0f0").pack(side=tk.TOP)

        ttk.Button(info_window, text="关闭", command=info_window.destroy).pack(side=tk.BOTTOM, pady=10)


def copy_to_clipboard(txt):
    root.clipboard_clear()
    root.clipboard_append(txt)
    messagebox.showinfo('提示', f'已复制：\n{txt}')


if __name__ == '__main__':
    try:
        from tkinterdnd2 import TkinterDnD, DND_FILES
        root = TkinterDnD.Tk()
    except ImportError:
        messagebox.showwarning('缺少库', 'pip install tkinterdnd2 后可支持拖拽文件')
        root = tk.Tk()

    app = App(root)
    app.update_ip_url()
    root.mainloop()