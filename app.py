#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Armbian Iter —— 局域网 APT 仓库服务（仅 Python 标准库）。

上传 Armbian 镜像（.tar.gz / .img）→ 只读挂载 → 按镜像内 dpkg 元数据把内核
三件套（linux-image/dtb/headers-edge-rockchip64）重打包为标准 .deb → 建立
Packages/Release 索引，通过 HTTP 提供局域网 apt 源。也可直接上传任意应用
程序 .deb 入池，客户端同样通过 apt 安装/升级。之后在任何设备上：

    echo 'deb [trusted=yes] http://<本机IP>:8090/apt ./' > /etc/apt/sources.list.d/armbian-iter.list
    sudo apt update && sudo apt upgrade

绝不自动重启；绝不写入 /root/armbain 工作区；/apt/* 匿名只读，管理接口需 token。
"""
import base64
import email.utils
import gzip
import hashlib
import hmac
import json
import logging
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import unquote

CONF_PATH = Path(os.environ.get("ITER_CONF", "/etc/armbian-iter.conf"))
BASE = Path(os.environ.get("ITER_BASE", "/srv/armbian-iter"))
REPO = BASE / "repo"
POOL = REPO / "pool"
ART_DIR = BASE / "artifacts"
LOG_DIR = BASE / "logs"
TMP = BASE / "tmp"
QUAR = BASE / "quarantine"
BOOT = Path("/boot")

KERNEL_PKGS = ("linux-image-edge-rockchip64", "linux-dtb-edge-rockchip64",
               "linux-headers-edge-rockchip64")
SCRIPTS = ("preinst", "postinst", "prerm", "postrm", "triggers")

GPT_LINUX_FS = uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4").bytes_le
GPT_LINUX_ROOT_ARM64 = uuid.UUID("b921b045-1df0-41c3-af44-4c6f280d3fae").bytes_le

JOB_LOCK = threading.Lock()
INDEX_LOCK = threading.Lock()

KVER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")

log = logging.getLogger("armliter")


def load_conf():
    CONF_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CONF_PATH.exists():
        return json.loads(CONF_PATH.read_text())
    conf = {"token": secrets.token_urlsafe(18), "port": 8090}
    CONF_PATH.write_text(json.dumps(conf, indent=2))
    os.chmod(CONF_PATH, 0o600)
    return conf


CONF = load_conf()


def run(cmd, timeout=3600, ok_rc=(0,)):
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=timeout)
    if p.returncode not in ok_rc:
        raise RuntimeError(f"命令失败 ({' '.join(str(c) for c in cmd)}):\n{p.stderr.strip() or p.stdout.strip()}")
    return p.stdout


def part_offset(img: Path) -> int:
    """镜像第一个 Linux 分区的字节偏移（GPT/MBR）。"""
    with open(img, "rb") as f:
        mbr = f.read(512)
        if mbr[510:512] != b"\x55\xaa":
            raise RuntimeError("无 MBR/GPT 签名，不是磁盘镜像")
        if f.read(512)[:8] == b"EFI PART":
            f.seek(512)
            hdr = f.read(92)
            ent_lba = int.from_bytes(hdr[72:80], "little")
            n_ent = int.from_bytes(hdr[80:84], "little")
            ent_sz = int.from_bytes(hdr[84:88], "little")
            f.seek(ent_lba * 512)
            for _ in range(min(n_ent, 128)):
                e = f.read(ent_sz)
                if len(e) < 48:
                    break
                if e[0:16] in (GPT_LINUX_FS, GPT_LINUX_ROOT_ARM64):
                    return int.from_bytes(e[32:40], "little") * 512
            raise RuntimeError("GPT 中未找到 Linux 分区")
        for i in range(4):
            e = mbr[446 + i * 16: 446 + (i + 1) * 16]
            if e[4] == 0x83:
                return int.from_bytes(e[8:12], "little") * 512
    raise RuntimeError("MBR 中未找到 Linux 分区")


class RoImage:
    def __init__(self, img: Path):
        self.img, self.loop, self.mnt = img, None, None

    def __enter__(self):
        off = part_offset(self.img)
        self.mnt = Path(f"/mnt/iter-{uuid.uuid4().hex[:8]}")
        self.mnt.mkdir()
        self.loop = run(["losetup", "-f", "--show", "-r", "-o", off, self.img]).strip()
        run(["mount", "-o", "ro", self.loop, self.mnt])
        return self.mnt

    def __exit__(self, *exc):
        try:
            run(["umount", self.mnt])
        finally:
            run(["losetup", "-d", self.loop])
            self.mnt.rmdir()
        return False


def host_fdtfile():
    # 服务器部署时目标板型与服务器无关，fdtfile 由 /etc/armbian-iter.conf 指定
    if CONF.get("fdtfile"):
        return CONF["fdtfile"]
    for line in (BOOT / "armbianEnv.txt").read_text().splitlines():
        if line.startswith("fdtfile="):
            return line.split("=", 1)[1].strip()
    return None


def ver_of(link: Path):
    try:
        n = os.path.basename(os.readlink(link))
    except OSError:
        return None
    for pre in ("vmlinuz-", "uInitrd-", "initrd.img-", "dtb-"):
        if n.startswith(pre):
            return n[len(pre):]
    return n


def stanza_of(status_text: str, pkg: str):
    for st in status_text.split("\n\n"):
        if re.search(rf"^Package: {re.escape(pkg)}$", st, re.M):
            return st
    raise RuntimeError(f"dpkg status 中找不到 {pkg}")


def repack_pkg(pkg: str, src_root: Path, kver: str, out_dir: Path) -> Path:
    """依据 src_root 内 dpkg 元数据把包重打包为 .deb，版本附加 +<kver>。"""
    info = src_root / "var/lib/dpkg/info"
    lst_f = info / f"{pkg}.list"
    if not lst_f.exists():
        raise RuntimeError(f"{src_root} 中缺少 {pkg}.list，无法重打包")
    # tar 对 -T 清单中的绝对路径按根目录解析（-C 不生效），必须转为相对路径
    lines = [l.lstrip("/") for l in lst_f.read_text().splitlines()
             if l.startswith("/") and l not in ("/", "/.",
                                                "/lib", "/lib64", "/bin", "/sbin")]
    if not lines:
        raise RuntimeError(f"{pkg}.list 为空")

    staging = TMP / f"pack-{uuid.uuid4().hex[:8]}"
    staging.mkdir(parents=True)
    fl = TMP / f"{staging.name}.files"
    fl.write_text("\n".join(lines) + "\n")
    tball = TMP / f"{staging.name}.tar"
    # --no-recursion：严格按 .list 归档，避免把别的包的文件（boot.scr 等）带进去
    run(["tar", "-C", src_root, "--no-recursion", "--ignore-failed-read",
         "-cf", tball, f"--files-from={fl}"], ok_rc=(0, 1))
    run(["tar", "-C", staging, "-xf", tball])
    tball.unlink()
    fl.unlink()

    orig_ver = next(l.split(":", 1)[1].strip() for l in stanza_of(
        (src_root / "var/lib/dpkg/status").read_text(), pkg).splitlines()
        if l.startswith("Version:"))
    new_ver = orig_ver if orig_ver.endswith("+" + kver) else f"{orig_ver}+{kver}"

    installed = sum(f.stat().st_size for f in staging.rglob("*") if f.is_file()) // 1024
    ctl, skip_cont = [], False
    for line in stanza_of((src_root / "var/lib/dpkg/status").read_text(), pkg).splitlines():
        if line.startswith(" "):
            if not skip_cont:
                ctl.append(line)
            continue
        key = line.split(":", 1)[0]
        skip_cont = key in ("Status", "Conffiles")
        if key in ("Status", "Conffiles"):
            continue
        ctl.append(f"Version: {new_ver}" if key == "Version" else
                   f"Installed-Size: {installed}" if key == "Installed-Size" else line)
    deb_dir = staging / "DEBIAN"
    deb_dir.mkdir()
    (deb_dir / "control").write_text("\n".join(ctl).strip() + "\n")
    for s in SCRIPTS:
        src = info / f"{pkg}.{s}"
        if src.exists():
            dst = deb_dir / s
            shutil.copy2(src, dst)
            os.chmod(dst, 0o755)
    cf = info / f"{pkg}.conffiles"
    if cf.exists():
        shutil.copy2(cf, deb_dir / "conffiles")

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{pkg}_{new_ver}_arm64.deb"
    run(["dpkg-deb", "--build", "-Zzstd", str(staging), str(out)])
    shutil.rmtree(staging)
    log.info("重打包 %s -> %s", pkg, out.name)
    return out


def deb_info(deb: Path):
    """读取 .deb 控制字段并做基本校验；损坏/非标准包抛 RuntimeError。"""
    out = run(["dpkg-deb", "-f", deb, "Package", "Version", "Architecture"])
    kv = dict(re.findall(r"^([A-Za-z-]+):\s*(.*)$", out, re.M))
    if not kv.get("Package") or not kv.get("Version"):
        raise RuntimeError(f"无法读取 {deb.name} 的控制字段，不是标准 .deb")
    return kv


def build_index():
    """重建 apt 仓库索引（Packages / Packages.gz / Release），原子替换。"""
    with INDEX_LOCK:
        entries, metas, archs = [], [], set()
        for deb in sorted(POOL.glob("*.deb")):
            try:
                fields = run(["dpkg-deb", "-f", deb, "Package", "Version", "Architecture",
                              "Maintainer", "Installed-Size", "Depends", "Provides",
                              "Description"]).strip()
                kv = dict(re.findall(r"^([A-Za-z-]+):\s*(.*)$", fields, re.M))
                if not kv.get("Package") or not kv.get("Version"):
                    raise RuntimeError("元数据不完整")
            except RuntimeError:
                # 单个坏包只隔离自身，绝不让它卡死整个索引重建（含内核流水线）
                QUAR.mkdir(parents=True, exist_ok=True)
                shutil.move(str(deb), str(QUAR / f"{time.strftime('%H%M%S')}-{deb.name}"))
                log.warning("池内损坏的 %s 已隔离到 quarantine/，不参与索引", deb.name)
                continue
            is_kernel = kv.get("Package") in KERNEL_PKGS
            data = deb.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            entry = fields + ("" if fields.endswith("\n") else "\n")
            entry += f"Filename: pool/{deb.name}\nSize: {len(data)}\nSHA256: {sha}\n"
            if "Section:" not in entry:
                sec = "kernel" if is_kernel else "utils"
                entry = entry.replace("Maintainer:", f"Section: {sec}\nMaintainer:", 1)
            entries.append(entry)
            ver = kv.get("Version", "?")
            archs.add(kv.get("Architecture") or "arm64")
            metas.append({"pkg": kv.get("Package", "?"), "version": ver,
                          "file": deb.name, "size": len(data),
                          "kind": "kernel" if is_kernel else "app",
                          "kver": ver.split("+")[-1] if is_kernel and "+" in ver else ""})
        pkgs = "\n".join(entries)
        for name, content in (("Packages", pkgs.encode()),
                              ("Packages.gz", gzip.compress(pkgs.encode(), 6))):
            tmp = REPO / (name + ".tmp")
            tmp.write_bytes(content)
            os.replace(tmp, REPO / name)

        def sums(name):
            data = (REPO / name).read_bytes()
            return (hashlib.md5(data).hexdigest(), hashlib.sha1(data).hexdigest(),
                    hashlib.sha256(data).hexdigest(), len(data))

        rel = ["Origin: armbian-iter", "Label: armbian-iter", "Suite: iter",
               "Codename: iter",
               "Architectures: " + (" ".join(sorted(archs)) or "arm64"),
               "Description: LAN apt repo: Armbian rockchip64 edge kernel trio + application debs",
               "Date: " + email.utils.formatdate(usegmt=True)]
        for field, idx in (("MD5Sum", 0), ("SHA1", 1), ("SHA256", 2)):
            rel.append(field + ":")
            for name in ("Packages", "Packages.gz"):
                s = sums(name)
                rel.append(f" {s[idx]} {s[3]} {name}")
        tmp = REPO / "Release.tmp"
        tmp.write_text("\n".join(rel) + "\n")
        os.replace(tmp, REPO / "Release")
        (REPO / "index-meta.json").write_text(json.dumps(metas, ensure_ascii=False, indent=1))
        log.info("仓库索引已更新：%d 个包", len(metas))


def pool_add(deb: Path):
    dest = POOL / deb.name
    if dest.exists():
        deb.unlink()
        return False
    os.rename(deb, dest)
    return True


# ---------- 工件流水线 ----------

def art_dir(aid):
    if not NAME_RE.match(aid):
        raise RuntimeError("非法工件 ID")
    return ART_DIR / aid


def meta_read(aid):
    return json.loads((art_dir(aid) / "meta.json").read_text())


def meta_write(aid, meta):
    (art_dir(aid) / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))


def set_state(aid, **kw):
    m = meta_read(aid)
    m.update(kw)
    meta_write(aid, m)


def process_artifact(aid):
    try:
        with JOB_LOCK:
            meta = meta_read(aid)
            d = art_dir(aid)
            img = d / "image.img"
            if not img.exists() and meta.get("orig"):
                set_state(aid, state="extracting")
                with tarfile.open(meta["orig"], "r:gz") as tf:
                    members = [m for m in tf.getmembers() if m.name.endswith(".img") and m.isfile()]
                    if len(members) != 1:
                        raise RuntimeError(f"压缩包内 .img 数量异常: {len(members)}")
                    with open(img, "wb") as out, tf.extractfile(members[0]) as src:
                        shutil.copyfileobj(src, out, 4 * 1024 * 1024)
                os.unlink(meta["orig"])
                set_state(aid, orig=None)

            set_state(aid, state="repacking")
            with RoImage(img) as mnt:
                kvers = sorted(p.name[len("vmlinuz-"):] for p in (mnt / "boot").glob("vmlinuz-*"))
                if len(kvers) != 1:
                    raise RuntimeError(f"镜像内内核数量异常: {kvers}，仅支持单内核镜像")
                kver = kvers[0]
                fdt = host_fdtfile()
                if not (mnt / "boot" / f"dtb-{kver}" / fdt).exists():
                    raise RuntimeError(f"镜像缺少本机 dtb（{fdtfile_msg(fdt)}），已拒绝入池")
                suite = None
                for line in (mnt / "etc/os-release").read_text().splitlines():
                    if line.startswith("PRETTY_NAME="):
                        suite = line.split("=", 1)[1].strip('"')
                debs = []
                for pkg in KERNEL_PKGS:
                    debs.append(repack_pkg(pkg, mnt, kver, d / "debs"))
            added = [p.name for p in sorted((d / "debs").glob("*.deb")) if pool_add(p)]
            build_index()
            set_state(aid, state="done", kver=kver, suite=suite, debs=added,
                      done_at=time.strftime("%F %T"))
            log.info("工件 %s 完成：内核 %s，入池 %s", aid, kver, added)
    except Exception as e:  # noqa: BLE001 — 后台线程必须收敛到 meta
        log.exception("工件 %s 处理失败", aid)
        try:
            set_state(aid, state="error", error=str(e))
        except OSError:
            pass


def fdtfile_msg(fdt):
    return fdt or "本机 armbianEnv.txt 未设置 fdtfile"


def spawn(aid):
    threading.Thread(target=process_artifact, args=(aid,), daemon=True).start()


def selfpack():
    """把本机已装内核重打包入池，作为可回滚基线。"""
    def work():
        try:
            with JOB_LOCK:
                kver = platform.release()
                out = TMP / f"self-{uuid.uuid4().hex[:6]}"
                out.mkdir(parents=True)
                debs = [repack_pkg(p, Path("/"), kver, out) for p in KERNEL_PKGS]
                added = [p.name for p in sorted(out.glob("*.deb")) if pool_add(p)]
                build_index()
                log.info("自打包完成（内核 %s）：入池 %s", kver, added)
        except Exception:  # noqa: BLE001
            log.exception("自打包失败")
    threading.Thread(target=work, daemon=True).start()


# ---------- 状态 ----------

def lan_ip():
    try:
        return subprocess.run(["hostname", "-I"], capture_output=True, text=True
                              ).stdout.split()[0]
    except (OSError, IndexError):
        return ""


def status():
    boot_v = ver_of(BOOT / "Image")
    running = platform.release()
    try:
        pool = json.loads((REPO / "index-meta.json").read_text())
    except (OSError, ValueError):
        pool = []
    arts = []
    if ART_DIR.is_dir():
        for d in sorted(ART_DIR.iterdir(), reverse=True):
            try:
                m = json.loads((d / "meta.json").read_text())
                arts.append({k: m.get(k) for k in ("id", "name", "state", "kver",
                                                   "suite", "error", "uploaded_at", "debs")})
            except (OSError, ValueError):
                continue
    du = shutil.disk_usage("/")
    return {
        "running": running, "boot_kernel": boot_v,
        "needs_reboot": boot_v is not None and running != boot_v,
        "pool": pool, "artifacts": arts,
        "disk_free_gb": round(du.free / 2**30, 1),
        "lan_ip": lan_ip(), "port": CONF["port"],
    }


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ArmbianIter/2.0"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    # -- 认证 --
    def _auth(self):
        tok = CONF.get("token")
        if not tok:
            return True
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            _, _, pw = base64.b64decode(h[6:]).decode().partition(":")
        except Exception:  # noqa: BLE001
            return False
        return hmac.compare_digest(pw, tok)

    def _deny(self):
        body = b'{"error":"unauthorized"}'
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="armbian-iter"')
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- 输出 --
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path):
        st = path.stat()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(st.st_size))
        self.end_headers()
        if self.command != "HEAD":
            with open(path, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1024 * 1024)

    def _body_json(self, limit=65536):
        n = int(self.headers.get("Content-Length", 0))
        if n > limit:
            raise RuntimeError("请求体过大")
        return json.loads(self.rfile.read(n) or b"{}")

    # -- 路由 --
    def _route(self):
        path = self.path.split("?")[0]
        if path.startswith("/apt/"):
            rel = unquote(path[len("/apt/"):])
            f = (REPO / rel).resolve()
            if not str(f).startswith(str(REPO.resolve())) or not f.is_file():
                return self._json(404, {"error": "not found"})
            return self._file(f)
        if not self._auth():
            return self._deny()
        if self.command in ("GET", "HEAD"):
            if path in ("/", "/index.html"):
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command == "GET":
                    self.wfile.write(body)
            elif path == "/api/status":
                self._json(200, status())
            elif path == "/api/logs":
                n = 200
                if "n=" in self.path:
                    try:
                        n = min(int(self.path.split("n=")[1].split("&")[0]), 1000)
                    except ValueError:
                        pass
                lines = []
                lf = LOG_DIR / "service.log"
                if lf.exists():
                    lines = lf.read_text(errors="replace").splitlines()[-n:]
                self._json(200, {"lines": lines})
            else:
                self._json(404, {"error": "not found"})
        elif self.command in ("POST", "PUT"):
            if path.startswith("/api/upload"):
                return self._upload()
            if path == "/api/import":
                req = self._body_json()
                return self._import_local(req.get("path", ""))
            if path == "/api/selfpack":
                if CONF.get("disable_selfpack"):
                    return self._json(403, {"error": "本服务器已禁用自打包：自打包的是服务器自身内核（x86），仅当服务器与目标机同板型时才启用"})
                selfpack()
                return self._json(202, {"started": True})
            if len(path.strip("/").split("/")) == 4 and path.startswith("/api/artifacts/") and path.endswith("/reprocess"):
                aid = path.strip("/").split("/")[2]
                d = art_dir(aid)
                if not d.is_dir():
                    return self._json(404, {"error": "无此工件"})
                if not (d / "image.img").exists():
                    return self._json(400, {"error": "工件内无 image.img，请重新上传"})
                set_state(aid, state="processing", error=None)
                spawn(aid)
                return self._json(202, {"id": aid})
            return self._json(404, {"error": "not found"})
        elif self.command == "DELETE":
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["api", "artifacts"]:
                d = art_dir(parts[2])
                if not d.is_dir():
                    return self._json(404, {"error": "无此工件"})
                shutil.rmtree(d)
                log.info("删除工件 %s", parts[2])
                return self._json(200, {"deleted": parts[2]})
            if len(parts) == 3 and parts[:2] == ["api", "pool"]:
                f = POOL / parts[2]
                if not NAME_RE.match(parts[2]) or not f.is_file():
                    return self._json(404, {"error": "无此文件"})
                f.unlink()
                build_index()
                log.info("删除池内 %s 并重建索引", parts[2])
                return self._json(200, {"deleted": parts[2]})
            return self._json(404, {"error": "not found"})
        else:
            self._json(405, {"error": "method not allowed"})

    def _upload(self):
        name = "upload.bin"
        if "name=" in self.path:
            name = os.path.basename(unquote(self.path.split("name=")[1].split("&")[0]))
        name = re.sub(r"[^A-Za-z0-9._+-]", "_", name) or "upload.bin"
        cl = int(self.headers.get("Content-Length", 0))
        if cl <= 0:
            return self._json(400, {"error": "缺少 Content-Length"})
        if name.endswith(".deb"):  # 应用/内核包：直接入池，客户端 apt install 即装
            if shutil.disk_usage("/").free < cl + 2 * 2**30:
                return self._json(507, {"error": "磁盘空间不足"})
            POOL.mkdir(parents=True, exist_ok=True)
            TMP.mkdir(parents=True, exist_ok=True)
            recv = TMP / f"recv-{uuid.uuid4().hex[:8]}"
            try:
                self._stream_to(recv, cl)
                try:
                    kv = deb_info(recv)  # 校验损坏包，避免污染索引
                except RuntimeError as e:
                    return self._json(400, {"error": str(e)})
                replaced = (POOL / name).exists()
                os.replace(recv, POOL / name)
            finally:
                recv.unlink(missing_ok=True)
            build_index()
            log.info("直接入池 %s（%s %s, %s）%s", name, kv.get("Package"),
                     kv.get("Version"), kv.get("Architecture"),
                     "，覆盖同名" if replaced else "")
            return self._json(200, {"added": name, "pkg": kv.get("Package"),
                                    "version": kv.get("Version"),
                                    "arch": kv.get("Architecture", "?"),
                                    "replaced": replaced})
        if shutil.disk_usage("/").free < cl + int(7.5 * 2**30):
            return self._json(507, {"error": f"磁盘空间不足（需约 {cl // 2**30 + 8} GB）"})
        aid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        d = ART_DIR / aid
        d.mkdir(parents=True)
        is_img = name.endswith(".img")
        dst = d / ("image.img" if is_img else "orig.tar.gz")
        self._stream_to(dst, cl)
        meta = {"id": aid, "name": name, "size": cl,
                "uploaded_at": time.strftime("%F %T"),
                "state": "processing", "orig": None if is_img else str(dst)}
        meta_write(aid, meta)
        spawn(aid)
        log.info("接收上传 %s (%.2f GB) → 工件 %s", name, cl / 2**30, aid)
        self._json(202, {"id": aid, "state": "processing"})

    def _stream_to(self, dst: Path, cl: int):
        h = hashlib.sha256()
        remaining = cl
        with open(dst, "wb") as f:
            while remaining:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise RuntimeError("上传中断")
                f.write(chunk)
                h.update(chunk)
                remaining -= len(chunk)
        return h.hexdigest()

    def _import_local(self, raw):
        src = Path(raw).resolve()
        if not src.is_file() or src.suffix not in (".gz", ".img", ".tgz", ".deb"):
            return self._json(400, {"error": "仅支持本地 .tar.gz/.tgz/.img/.deb"})
        if src.suffix == ".deb":
            try:
                kv = deb_info(src)  # 校验后再入池
            except RuntimeError as e:
                return self._json(400, {"error": str(e)})
            dst = POOL / src.name
            # /root 工作区一律复制；/tmp、/var/tmp 下可移动，节省一次拷贝
            if str(src).startswith("/root/") or not str(src).startswith(("/tmp/", "/var/tmp/")):
                shutil.copy2(src, dst)
            else:
                shutil.move(src, dst)
            build_index()
            log.info("导入应用包 %s（%s %s）", src.name, kv.get("Package"), kv.get("Version"))
            return self._json(200, {"added": src.name, "pkg": kv.get("Package"),
                                    "version": kv.get("Version"),
                                    "arch": kv.get("Architecture", "?")})
        aid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        d = ART_DIR / aid
        d.mkdir(parents=True)
        # 工作区（/root/armbain）内文件一律复制，绝不移动或删除
        movable = str(src).startswith(("/var/tmp/", "/tmp/")) and not str(src).startswith("/root/")
        is_img = src.suffix == ".img"
        dst = d / ("image.img" if is_img else "orig.tar.gz")
        if movable:
            os.rename(src, dst)
        else:
            shutil.copy2(src, dst)
        meta = {"id": aid, "name": src.name, "size": dst.stat().st_size,
                "uploaded_at": time.strftime("%F %T"), "state": "processing",
                "orig": None if is_img else str(dst)}
        meta_write(aid, meta)
        spawn(aid)
        log.info("导入本地 %s → 工件 %s（%s）", src, aid, "移动" if movable else "复制")
        self._json(202, {"id": aid, "state": "processing"})

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = lambda self: self._route()


PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>Armbian Iter · 系统迭代仓库</title><link rel="icon" href="data:,">
<style>
:root{--bg:#12141a;--card:#1c1f27;--line:#2b2f3a;--fg:#e6e8ee;--dim:#9aa1af;--acc:#4f8cff;--ok:#3fb96f;--warn:#e0a83c;--err:#e05c5c}
*{box-sizing:border-box}body{margin:0;font:14px/1.6 system-ui,-apple-system,"Noto Sans CJK SC",sans-serif;background:var(--bg);color:var(--fg)}
.wrap{max-width:1080px;margin:0 auto;padding:24px 16px 60px}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--dim);font-size:12px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:16px}
.card h2{font-size:13px;margin:0 0 12px;color:var(--dim);font-weight:600;text-transform:uppercase;letter-spacing:.05em}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}
.kv{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.kv .k{color:var(--dim);font-size:12px}.kv .v{font-size:15px;font-weight:600;margin-top:2px;word-break:break-all}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;margin-left:6px;vertical-align:middle}
.b-ok{background:#12351f;color:var(--ok)}.b-warn{background:#3a2f12;color:var(--warn)}.b-err{background:#3a1515;color:var(--err)}.b-dim{background:#22252e;color:var(--dim)}
table{width:100%;border-collapse:collapse;font-size:13px}th{color:var(--dim);text-align:left;font-weight:500;padding:6px 8px;border-bottom:1px solid var(--line)}td{padding:8px;border-bottom:1px solid var(--line);word-break:break-all}
button{background:var(--acc);border:0;color:#fff;border-radius:6px;padding:6px 12px;font-size:13px;cursor:pointer;margin:2px}
button:hover{filter:brightness(1.1)}button.ghost{background:transparent;border:1px solid var(--line);color:var(--fg)}button.warn{background:#7a3b3b}
input[type=file],input[type=text]{background:var(--bg);border:1px solid var(--line);color:var(--fg);border-radius:6px;padding:6px 10px;max-width:420px}
#bar{height:6px;background:var(--line);border-radius:3px;margin-top:10px;overflow:hidden;display:none}#barfill{height:100%;width:0;background:var(--acc)}
pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:12px;font-size:12px;max-height:320px;overflow:auto;white-space:pre-wrap}
code{background:var(--bg);border:1px solid var(--line);padding:1px 6px;border-radius:4px;font-size:12px}
.muted{color:var(--dim)}.mono{font-family:ui-monospace,monospace}
</style></head><body><div class="wrap">
<h1>Armbian Iter <span class="badge b-dim">LAN apt repo</span></h1>
<div class="sub">上传镜像 → 自动重打包内核 .deb → 局域网 apt 源 · Skysi-X5 (RK3588) · 绝不自动重启</div>

<div class="card"><h2>状态</h2><div class="grid" id="stat"></div></div>

<div class="card"><h2>客户端接入（apt 工作流）</h2><div id="howto">加载中…</div></div>

<div class="card"><h2>上传 / 导入</h2>
<input type="file" id="file" accept=".tar.gz,.tgz,.img,.deb">
<button onclick="up()">上传并处理</button>
<div id="bar"><div id="barfill"></div></div>
<div class="muted" style="margin-top:8px">镜像（.img/.tar.gz）走后台重打包；直接上传 .deb（内核包或任意应用包）校验后立即入池，客户端 <code>apt update</code> 即可安装。</div>
<div style="margin-top:10px" class="muted">服务器本地路径导入：
<input type="text" id="lpath" placeholder="/var/tmp/xxx.img 或 .tar.gz 或 .deb" style="max-width:320px">
<button class="ghost" onclick="imp()">导入</button>
<button class="ghost" onclick="selfpack()" title="把本机当前内核打包入池，作为回滚基线">自打包当前内核</button>
<div id="upmsg" style="margin-top:6px"></div></div>
</div>

<div class="card"><h2>仓库中的包</h2><div id="pool">加载中…</div></div>

<div class="card"><h2>镜像工件</h2><div id="arts">加载中…</div></div>

<div class="card"><h2>服务日志</h2><pre id="logv">—</pre><button class="ghost" onclick="loadLog()">刷新</button></div>
</div>

<script>
const $=q=>document.querySelector(q);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(p,o){const r=await fetch(p,o);const j=await r.json().catch(()=>({error:'HTTP '+r.status}));if(!r.ok)throw new Error(j.error||('HTTP '+r.status));return j}
async function load(){
 try{
  const s=await api('/api/status');
  const kv=(k,v)=>'<div class="kv"><div class="k">'+k+'</div><div class="v">'+v+'</div></div>';
  $('#stat').innerHTML=
   kv('正在运行的内核',esc(s.running))+
   kv('下次默认启动',esc(s.boot_kernel)+(s.boot_kernel?(s.needs_reboot?' <span class="badge b-warn">待重启生效</span>':' <span class="badge b-ok">一致</span>'):''))+
   kv('仓库包数量',String((s.pool||[]).length))+
   kv('磁盘剩余',s.disk_free_gb+' GB');
  const ip=s.lan_ip||'&lt;本机IP&gt;';
  $('#howto').innerHTML=
   '<div class="muted">任意局域网设备（含本机）执行一次：</div>'+
   '<pre>echo "deb [trusted=yes] http://'+ip+':'+s.port+'/apt ./" | sudo tee /etc/apt/sources.list.d/armbian-iter.list\\nsudo apt update\\napt list --upgradable\\nsudo apt upgrade   # 升级内核后重启生效</pre>'+
   '<div class="muted">回滚示例：<code>sudo apt install linux-image-edge-rockchip64=26.11.0-trunk+7.2.4 linux-dtb-edge-rockchip64=26.11.0-trunk+7.2.4 linux-headers-edge-rockchip64=26.11.0-trunk+7.2.4 --allow-downgrades</code></div>'+
   '<div class="muted" style="margin-top:6px">安装/升级应用包：<code>sudo apt install 包名</code>（上传入池的任意 .deb 均可，<code>apt policy 包名</code> 可查可选版本）</div>';
  const pr=(s.pool||[]).map(p=>'<tr><td class="mono">'+esc(p.pkg)+'</td><td class="mono">'+esc(p.version)+'</td><td>'+(p.kver?'<span class="badge b-dim">'+esc(p.kver)+'</span>':(p.kind==='app'?'<span class="badge b-dim">应用</span>':''))+'</td><td>'+(p.size/2**20).toFixed(1)+' MB</td><td><button class="warn" onclick="delp(\\''+esc(p.file)+'\\')">删除</button></td></tr>').join('');
  $('#pool').innerHTML=(s.pool||[]).length?'<table><tr><th>包名</th><th>版本</th><th>类型</th><th>大小</th><th></th></tr>'+pr+'</table>':'<span class="muted">仓库为空：上传镜像或点“自打包当前内核”，或直接上传应用 .deb</span>';
  const ar=(s.artifacts||[]).map(a=>{
    const stt={done:'<span class="badge b-ok">完成</span>',processing:'<span class="badge b-warn">处理中</span>',extracting:'<span class="badge b-warn">解压中</span>',repacking:'<span class="badge b-warn">重打包中</span>',error:'<span class="badge b-err">失败</span>'}[a.state]||a.state;
    return '<tr><td>'+esc(a.name)+'<br><span class="muted mono">'+a.id+'</span></td><td>'+(a.kver?esc(a.kver):stt)+(a.suite?'<br><span class="muted">'+esc(a.suite)+'</span>':'')+(a.error?'<br><span style="color:var(--err)">'+esc(a.error)+'</span>':'')+'</td><td>'+((a.debs||[]).length?a.debs.map(d=>esc(d)).join('<br>'):'')+'</td><td>'+(a.state==='done'?'':'')+'<button class="warn" onclick="dela(\\''+a.id+'\\')">删除</button></td></tr>';
  }).join('');
  $('#arts').innerHTML=ar?'<table><tr><th>工件</th><th>状态</th><th>入池 deb</th><th></th></tr>'+ar+'</table>':'<span class="muted">无</span>';
 }catch(e){$('#stat').innerHTML='<div class="kv"><div class="v" style="color:var(--err)">'+esc(e.message)+'</div></div>'}
}
function up(){
 const f=$('#file').files[0];if(!f)return alert('先选择文件');
 const x=new XMLHttpRequest();$('#bar').style.display='block';$('#upmsg').textContent='上传中…';
 x.open('POST','/api/upload?name='+encodeURIComponent(f.name));
 x.upload.onprogress=e=>{$('#barfill').style.width=(e.loaded/e.total*100)+'%'};
 x.onload=()=>{const j=JSON.parse(x.responseText);if(x.status>299)return alert('失败: '+(j.error||x.status));$('#upmsg').textContent=j.id?('已接收 '+j.id+'，后台解包/重打包中（约 2-5 分钟）'):('已入池 '+(j.pkg||j.added)+' '+(j.version||'')+(j.replaced?'（覆盖同名）':'')+'，客户端 apt update 后可安装');setTimeout(load,1500)};
 x.onerror=()=>{$('#upmsg').textContent='网络错误'};
 x.send(f);
}
async function imp(){const p=$('#lpath').value.trim();if(!p)return;try{const j=await api('/api/import',{method:'POST',body:JSON.stringify({path:p})});$('#upmsg').textContent=j.id?('已导入 '+j.id+'，后台解包/重打包中（约 2-5 分钟）'):('已入池 '+(j.pkg||j.added)+' '+(j.version||'')+'，客户端 apt update 后可安装');setTimeout(load,1500)}catch(e){alert(e.message)}}
async function selfpack(){if(!confirm('把本机当前内核重打包入池（回滚基线）？'))return;try{await api('/api/selfpack',{method:'POST'});$('#upmsg').textContent='自打包已开始…';setTimeout(load,2000)}catch(e){alert(e.message)}}
async function delp(f){if(!confirm('从仓库删除 '+f+' 并重建索引？'))return;try{await api('/api/pool/'+f,{method:'DELETE'})}catch(e){alert(e.message)}load()}
async function dela(id){if(!confirm('删除工件 '+id+'（已入池的 deb 不受影响）？'))return;try{await api('/api/artifacts/'+id,{method:'DELETE'})}catch(e){alert(e.message)}load()}
async function loadLog(){try{const j=await api('/api/logs?n=150');$('#logv').textContent=j.lines.join('\\n')||'(空)'}catch(e){}}
load();loadLog();setInterval(load,5000);setInterval(loadLog,15000);
</script></body></html>"""


def main():
    for d in (BASE, REPO, POOL, ART_DIR, LOG_DIR, TMP, QUAR):
        d.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(),
                  RotatingFileHandler(LOG_DIR / "service.log", maxBytes=5 * 2**20, backupCount=2)],
    )
    if not (REPO / "Packages").exists():
        build_index()
    srv = ThreadingHTTPServer(("0.0.0.0", int(CONF["port"])), Handler)
    srv.daemon_threads = True
    log.info("Armbian Iter v2 监听 0.0.0.0:%s（/apt 匿名只读；管理接口 token=%s）",
             CONF["port"], "已启用" if CONF.get("token") else "未启用")
    srv.serve_forever()


if __name__ == "__main__":
    main()
