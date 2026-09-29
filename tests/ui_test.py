#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Armbian Iter Web UI 端到端测试(Playwright + 真实 Chromium)。

自起沙箱服务(ITER_CONF / ITER_BASE 环境变量指到临时目录,不碰 /etc 与 /srv),
用浏览器完整跑通:页面加载、状态渲染、接入命令块、.deb 上传入池、同名覆盖、
坏包拒收、本地路径导入、删除包并重建索引、apt 索引一致性、日志面板。

任何 console error / pageerror 都判失败——曾经页面因 JS 字符串跨行导致整段
脚本解析失败(所有按钮报 up is not defined),此门槛用于防止同类回归。

运行:
    pip install playwright && playwright install --with-deps chromium
    python3 tests/ui_test.py
"""
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app.py"
TOKEN = "ui-test-token"
PASSED = []


def check(name, fn):
    fn()
    PASSED.append(name)
    print(f"  ✓ {name}")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_deb(out: Path, name: str, ver: str, arch: str = "all") -> Path:
    root = out.parent / f"pkg-{name}-{ver}-{uuid.uuid4().hex[:4]}"
    (root / "DEBIAN").mkdir(parents=True)
    (root / "usr/bin").mkdir(parents=True)
    (root / "DEBIAN" / "control").write_text(
        f"Package: {name}\nVersion: {ver}\nArchitecture: {arch}\n"
        f"Maintainer: ui-test <t@t>\nInstalled-Size: 1\nDescription: ui test pkg\n")
    binf = root / "usr/bin" / name
    binf.write_text(f"#!/bin/sh\necho {name}-{ver}\n")
    binf.chmod(0o755)
    subprocess.run(["dpkg-deb", "--build", str(root), str(out)],
                   check=True, capture_output=True)
    shutil.rmtree(root)
    return out


def main():
    tmp = Path(tempfile.mkdtemp(prefix="iter-ui-"))
    port = free_port()
    (tmp / "conf.json").write_text(json.dumps({"token": TOKEN, "port": port}))
    env = dict(os.environ, ITER_CONF=str(tmp / "conf.json"),
               ITER_BASE=str(tmp / "srv"))
    proc = subprocess.Popen([sys.executable, str(APP)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                if proc.poll() is not None:
                    raise RuntimeError(f"服务进程启动即退出(rc={proc.returncode})")
                time.sleep(0.2)
        else:
            raise RuntimeError("服务未在 10s 内就绪")

        good1 = make_deb(tmp / "helloui_1.0_all.deb", "helloui", "1.0")
        good2 = make_deb(tmp / "fakeui_0.1_all.deb", "fakeui", "0.1")
        bad = tmp / "broken.deb"
        bad.write_bytes(os.urandom(4096))

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(
                http_credentials={"username": "x", "password": TOKEN})
            page = ctx.new_page()
            console_errors, page_errors = [], []
            page.on("console", lambda m: console_errors.append(m.text)
                    if m.type == "error" else None)
            page.on("pageerror", lambda e: page_errors.append(str(e)))

            print("— 页面加载与 JS 健康 —")
            page.goto(base + "/", wait_until="networkidle")
            check("页面标题", lambda: expect(page).to_have_title(
                "Armbian Iter · 系统迭代仓库"))
            check("状态卡片渲染", lambda: expect(page.locator("#stat")).to_contain_text(
                "正在运行的内核"))
            check("接入命令块完整(换行修复回归)", lambda: (
                expect(page.locator("#howto pre")).to_contain_text(
                    "sudo tee /etc/apt/sources.list.d/armbian-iter.list"),
                expect(page.locator("#howto pre")).to_contain_text("sudo apt update")))

            print("— .deb 直传入池 —")
            page.set_input_files("#file", str(good1))
            page.get_by_role("button", name="上传并处理").click()
            check("上传反馈含包信息", lambda: expect(page.locator("#upmsg")).to_contain_text(
                "已入池 helloui 1.0", timeout=10000))
            check("池列表出现该包", lambda: expect(page.locator("#pool")).to_contain_text(
                "helloui", timeout=10000))
            check("显示「应用」类型标记", lambda: expect(page.locator("#pool")).to_contain_text(
                "应用"))

            print("— 同名覆盖 —")
            page.set_input_files("#file", str(good1))
            page.get_by_role("button", name="上传并处理").click()
            check("覆盖提示", lambda: expect(page.locator("#upmsg")).to_contain_text(
                "覆盖同名", timeout=10000))

            print("— 坏包拒收 —")
            alerts = []
            page.once("dialog", lambda d: (alerts.append(d.message), d.accept()))
            page.set_input_files("#file", str(bad))
            page.get_by_role("button", name="上传并处理").click()
            check("坏包不入池", lambda: expect(page.locator("#pool")).not_to_contain_text(
                "broken", timeout=5000))
            page.wait_for_timeout(2000)  # 等 XHR 完成触发 alert
            assert any("失败" in m for m in alerts), f"alert 内容异常: {alerts}"
            # 坏包 400 属预期拒收;浏览器会把非 2xx 响应一律记为资源加载错误,
            # 此刻之前只允许出现这一类,之后必须零错误
            for m in console_errors:
                assert "Failed to load resource" in m and "400" in m, \
                    f"意外控制台错误: {m}"
            allowed_errors = len(console_errors)
            PASSED.append("坏包 alert 报错")
            print("  ✓ 坏包 alert 报错")

            print("— 本地路径导入 —")
            page.fill("#lpath", str(good2))
            page.get_by_role("button", name="导入", exact=True).click()
            check("导入反馈", lambda: expect(page.locator("#upmsg")).to_contain_text(
                "已入池 fakeui 0.1", timeout=10000))
            check("池内出现第二个包", lambda: expect(page.locator("#pool")).to_contain_text(
                "fakeui"))

            print("— apt 索引一致性 —")
            r = ctx.request.get(base + "/apt/Packages")
            assert r.ok and "helloui" in r.text() and "fakeui" in r.text(), \
                f"apt 索引缺包: {r.status}"
            PASSED.append("Packages 索引含两包")
            print("  ✓ Packages 索引含两包")

            print("— 上传中断韧性(传一半掐断连接,不裸崩、自动清理)—")
            sk = socket.create_connection(("127.0.0.1", port))
            auth = base64.b64encode(f"x:{TOKEN}".encode()).decode()
            sk.sendall((f"POST /api/upload?name=cut.img HTTP/1.1\r\nHost: t\r\n"
                        f"Authorization: Basic {auth}\r\n"
                        f"Content-Length: 50000000\r\n\r\n").encode())
            sk.sendall(b"x" * 100000)
            sk.close()  # 硬断线
            time.sleep(1.5)
            assert proc.poll() is None, "断线导致服务进程崩溃"
            r3 = ctx.request.get(base + "/api/status")
            assert r3.ok, f"断线后 status 不可用: {r3.status}"
            PASSED.append("断线后服务存活")
            print("  ✓ 断线后服务存活")
            arts = tmp / "srv" / "artifacts"
            for dd in (arts.iterdir() if arts.exists() else []):
                assert (dd / "meta.json").exists(), f"僵尸工件目录: {dd}"
            PASSED.append("断线上传自动清理")
            print("  ✓ 断线上传自动清理")
            r5 = ctx.request.get(base + "/api/logs?n=300")
            assert "上传中断已放弃" in r5.text(), "中断未记入服务日志"
            PASSED.append("中断记入日志")
            print("  ✓ 中断记入日志")

            print("— 非法请求体不裸崩 —")
            r4 = ctx.request.post(base + "/api/import", data="{bad json",
                                  headers={"Content-Type": "application/json"})
            assert r4.status == 400 and "error" in r4.json(), \
                f"非法 JSON 应返回 400: {r4.status}"
            PASSED.append("非法请求体返回 400")
            print("  ✓ 非法请求体返回 400")

            print("— 删除池内包 —")
            page.once("dialog", lambda d: d.accept())
            page.locator("#pool tr", has_text="fakeui").get_by_role(
                "button", name="删除").click()
            check("表格行消失", lambda: expect(page.locator("#pool")).not_to_contain_text(
                "fakeui", timeout=10000))
            r2 = ctx.request.get(base + "/apt/Packages")
            assert "fakeui" not in r2.text(), "删除后索引仍含 fakeui"
            PASSED.append("删除后索引即时重建")
            print("  ✓ 删除后索引即时重建")

            print("— 日志面板 —")
            page.get_by_role("button", name="刷新", exact=True).click()
            check("日志非空", lambda: expect(page.locator("#logv")).not_to_have_text(
                "(空)", timeout=10000))

            print("— JS 健康门槛(防同类回归)—")
            assert not page_errors, f"页面 JS 异常: {page_errors}"
            unexpected = console_errors[allowed_errors:]
            assert not unexpected, f"控制台错误: {unexpected}"
            PASSED.append("零 pageerror")
            print("  ✓ 零 pageerror")
            PASSED.append("零 console error")
            print("  ✓ 零 console error")

            browser.close()
        print(f"\n全部 {len(PASSED)} 项通过")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
