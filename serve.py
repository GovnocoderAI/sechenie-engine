#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
serve.py — раздача приложения «Стереометрия».

  python serve.py                 локальная сеть + браузер
  python serve.py --qr            + QR-код для телефона
  python serve.py --tunnel        публичная ссылка (перебор + ПРОВЕРКА содержимого)
  python serve.py --tunnel bore   конкретный провайдер: bore | lhr | pinggy | cf | serveo
  python serve.py --firewall      правило брандмауэра Windows (от администратора)

Внешних зависимостей нет. bore скачивается автоматически, остальное — через системный ssh.
"""
import argparse, http.server, importlib, io, ipaddress, json, os, platform, queue, re
import shutil, socket, socketserver, stat, subprocess, sys, tarfile, threading
import urllib.request, webbrowser, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INDEX = "stereo.html"
MARKER = "STEREO-APP-OK"          # метка в <meta> приложения — по ней проверяем туннель

def c(t, col=""):
    if os.name == "nt": os.system("")
    return f"\033[{ {'g':'92','y':'93','b':'96','r':'91','d':'90'}.get(col,'0') }m{t}\033[0m"

# ---------------------------------------------------------------- сеть
def lan_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0]); s.close()
    except OSError: pass
    try:
        for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET): ips.add(i[4][0])
    except socket.gaierror: pass
    out = []
    for ip in ips:
        try:
            a = ipaddress.ip_address(ip)
            if a.is_private and not a.is_loopback: out.append(ip)
        except ValueError: pass
    return sorted(out)

def free_port(p):
    with socket.socket() as s:
        try: s.bind(("", p)); return p
        except OSError: s.bind(("", 0)); return s.getsockname()[1]

class Handler(http.server.SimpleHTTPRequestHandler):
    index, quiet = INDEX, False
    def __init__(self, *a, **kw): super().__init__(*a, directory=str(ROOT), **kw)
    def do_GET(self):
        if self.path in ("/", "/index.html"): self.path = "/" + Handler.index
        return super().do_GET()
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()
    def log_message(self, f, *a):
        if not Handler.quiet: print(c(f"  {self.address_string()} → {f % a}", "d"))

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True; daemon_threads = True

def firewall(port):
    if platform.system() != "Windows":
        print(c("правило нужно только в Windows", "y")); return
    try:
        subprocess.check_call(["netsh","advfirewall","firewall","add","rule",
            f"name=Stereo {port}","dir=in","action=allow","protocol=TCP",f"localport={port}"])
        print(c("✓ правило брандмауэра добавлено", "g"))
    except Exception as e:
        print(c(f"не вышло ({e}) — нужен терминал от администратора", "r"))

# ---------------------------------------------------------------- bore
def get_bore():
    exe = shutil.which("bore") or shutil.which("bore.exe")
    if exe: return Path(exe)
    local = ROOT / ("bore.exe" if os.name == "nt" else "bore")
    if local.exists(): return local
    sysname, mach = platform.system(), platform.machine().lower()
    want = {("Windows","amd64"):"x86_64-pc-windows-msvc",
            ("Windows","x86_64"):"x86_64-pc-windows-msvc",
            ("Linux","x86_64"):"x86_64-unknown-linux-musl",
            ("Linux","aarch64"):"aarch64-unknown-linux-musl",
            ("Darwin","arm64"):"aarch64-apple-darwin",
            ("Darwin","x86_64"):"x86_64-apple-darwin"}.get((sysname, mach))
    if not want:
        print(c(f"  нет сборки bore для {sysname}/{mach}", "y")); return None
    try:
        print(c("· качаю bore …", "d"))
        api = "https://api.github.com/repos/ekzhang/bore/releases/latest"
        req = urllib.request.Request(api, headers={"User-Agent": "stereo"})
        rel = json.load(urllib.request.urlopen(req, timeout=25))
        url = next(a["browser_download_url"] for a in rel["assets"] if want in a["name"])
        blob = urllib.request.urlopen(url, timeout=90).read()
        if url.endswith(".zip"):
            z = zipfile.ZipFile(io.BytesIO(blob))
            name = next(n for n in z.namelist() if n.endswith(("bore", "bore.exe")))
            local.write_bytes(z.read(name))
        else:
            t = tarfile.open(fileobj=io.BytesIO(blob))
            name = next(n for n in t.getnames() if n.endswith(("bore", "bore.exe")))
            local.write_bytes(t.extractfile(name).read())
        local.chmod(local.stat().st_mode | stat.S_IEXEC)
        return local
    except Exception as e:
        print(c(f"  не удалось скачать bore: {e}", "y")); return None

def get_cloudflared():
    p = shutil.which("cloudflared") or shutil.which("cloudflared.exe")
    if p: return Path(p)
    url = {("Windows","AMD64"):"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe",
           ("Linux","x86_64"):"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
           ("Linux","aarch64"):"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64"
          }.get((platform.system(), platform.machine()))
    if not url: return None
    exe = ROOT / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
    if not exe.exists():
        print(c("· качаю cloudflared …", "d")); urllib.request.urlretrieve(url, exe)
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe

# ---------------------------------------------------------------- провайдеры
def ssh_base():
    null = "NUL" if os.name == "nt" else "/dev/null"
    return ["ssh","-o","StrictHostKeyChecking=no","-o",f"UserKnownHostsFile={null}",
            "-o","ServerAliveInterval=30","-o","ExitOnForwardFailure=yes"]

def plan(port):
    s = ssh_base()
    return [
      ("bore",   "bore.pub — TCP-туннель, без регистрации", "bore",
       r"listening at bore\.pub:(\d+)"),
      ("lhr",    "localhost.run — SSH, без регистрации",
       s + ["-R", f"80:localhost:{port}", "nokey@localhost.run"], r"https://[-\w.]+\.lhr\.life"),
      ("pinggy", "pinggy.io — SSH, сессия ~60 мин",
       s + ["-p","443","-R", f"0:localhost:{port}", "a.pinggy.io"],
       r"https://[-\w.]+\.(?:pinggy\.link|free\.pinggy\.link)"),
      ("cf",     "cloudflared — в РФ нестабилен", "cf", r"https://[-\w]+\.trycloudflare\.com"),
      ("serveo", "serveo.net — может потребовать аккаунт",
       s + ["-R", f"80:localhost:{port}", "serveo.net"], r"https://[-\w.]+\.serveo\.net"),
    ]

def verify(url, tries=6):
    """Проверяем, что по ссылке отдаётся именно приложение, а не страница входа."""
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (stereo-check)",
                "bypass-tunnel-reminder": "1"})
            body = urllib.request.urlopen(req, timeout=12).read(200_000).decode("utf-8", "ignore")
            if MARKER in body:
                return True
            if i == tries - 1:
                print(c("  ссылка отвечает, но отдаёт не приложение (вход/заглушка) — пропускаю", "y"))
        except Exception:
            pass
        threading.Event().wait(2.5)
    return False

def run_provider(key, title, cmd, rx, port, wait=30):
    if key == "bore":
        exe = get_bore()
        if not exe: return None, None
        cmd = [str(exe), "local", str(port), "--to", "bore.pub"]
    elif key == "cf":
        exe = get_cloudflared()
        if not exe: return None, None
        cmd = [str(exe), "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"]
    elif not shutil.which("ssh"):
        print(c("  ssh не найден (Windows: Параметры → Приложения → Дополнительные компоненты → Клиент OpenSSH)", "r"))
        return None, None
    print(c(f"· пробую {title} …", "d"))
    try:
        pr = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                              errors="ignore", bufsize=1)
    except Exception as e:
        print(c(f"  не запустилось: {e}", "y")); return None, None
    q = queue.Queue()
    def rd():
        for line in pr.stdout:
            m = re.search(rx, line)
            if m: q.put(m.group(1) if m.groups() else m.group(0)); break
        for _ in pr.stdout: pass
    threading.Thread(target=rd, daemon=True).start()
    try:
        got = q.get(timeout=wait)
    except queue.Empty:
        pr.terminate(); print(c("  не ответил вовремя", "y")); return None, None
    url = f"http://bore.pub:{got}" if key == "bore" else got
    print(c(f"  получено: {url} — проверяю …", "d"))
    if verify(url):
        return pr, url
    pr.terminate(); return None, None

def start_tunnel(which, port):
    lst = plan(port)
    order = lst if which in (None, "auto") else [p for p in lst if p[0] == which]
    for key, title, cmd, rx in order:
        pr, url = run_provider(key, title, cmd, rx, port)
        if url:
            print("\n" + c(f"  ✓ ПУБЛИЧНАЯ ССЫЛКА ({key}): {url}", "g"))
            print(c("    работает, пока открыт этот терминал\n", "d"))
            return pr, url
    print(c("\n  Ни один туннель не отдал приложение. Используй локальную сеть "
            "или выложи файл на GitHub Pages.\n", "r"))
    return None, None

def qr(url):
    try:
        import qrcode
    except ImportError:
        subprocess.check_call([sys.executable,"-m","pip","install","-q","--disable-pip-version-check","qrcode"])
        import qrcode
    q = qrcode.QRCode(border=1); q.add_data(url); q.make(); q.print_ascii(invert=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-p","--port", type=int, default=8000)
    ap.add_argument("-f","--file", default=INDEX)
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--qr", action="store_true")
    ap.add_argument("--tunnel", nargs="?", const="auto",
                    choices=["auto","bore","lhr","pinggy","cf","serveo"])
    ap.add_argument("--firewall", action="store_true")
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    if not (ROOT / a.file).exists():
        print(c(f"нет файла {a.file} в {ROOT}", "r")); sys.exit(1)
    if MARKER not in (ROOT / a.file).read_text(encoding="utf-8", errors="ignore"):
        print(c("предупреждение: в html нет метки STEREO-APP-OK, проверка туннеля отключится", "y"))
    port = free_port(a.port)
    Handler.index, Handler.quiet = a.file, a.quiet
    if a.firewall: firewall(port)

    httpd = Server(("127.0.0.1" if a.local else "0.0.0.0", port), Handler)
    print(c("\n  СТЕРЕОМЕТРИЯ · сервер запущен", "b"))
    print(f"  папка:   {ROOT}")
    print(f"  этот ПК: {c(f'http://localhost:{port}/','g')}")
    ips = [] if a.local else lan_ips()
    for ip in ips: print(f"  в сети:  {c(f'http://{ip}:{port}/','g')}   ← открой на телефоне")
    if a.qr and ips: print(); qr(f"http://{ips[0]}:{port}/")

    tun = None
    if a.tunnel:
        threading.Event().wait(0.4)
        tun, url = start_tunnel(a.tunnel, port)
        if url and a.qr: qr(url)
    print(c("  Ctrl+C — остановить\n", "d"))
    if not a.no_open:
        threading.Timer(.6, lambda: webbrowser.open(f"http://localhost:{port}/")).start()
    try: httpd.serve_forever()
    except KeyboardInterrupt: print(c("\n  остановлено", "y"))
    finally:
        httpd.server_close()
        if tun: tun.terminate()

if __name__ == "__main__":
    main()