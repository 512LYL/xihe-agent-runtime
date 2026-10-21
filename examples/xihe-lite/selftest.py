# -*- coding: utf-8 -*-
"""xihe-lite 自检脚本（**全程离线运行，不接触任何打印机**）

跑法：
    python selftest.py
    python selftest.py -v          # 打印每个用例的细节
    python selftest.py --json      # 机器可读

覆盖：
    配置/目录 → 暂存状态机 → 演练链路 → 真机口径（脚本替身）→ 失败/未确认/
    取消三种归档 → 结果映射表 → 重投 → 前置自检一行结论 → 服务注册表 →
    控制台 HTTP 冒烟 → 全量 py_compile → 隐私清洗扫描

所有"打印"都走 `SimulatedBackend` / `ScriptedBackend`（后者只是声称自己是真机，
用来断言 `printed → _已打印` 分支），**没有一处 import pywin32 或提交真实任务**。
"""
import argparse
import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))

from xihe import config as xihe_config                      # noqa: E402
from xihe.printing import backend as B                      # noqa: E402
from xihe.printing import preflight as PF                   # noqa: E402
from xihe.printing import states                            # noqa: E402
from xihe.printing.driver import PrintDriver                # noqa: E402
from xihe.printing.staging import Staging                   # noqa: E402

CASES = []
_TMPDIRS = []


def case(fn):
    CASES.append(fn)
    return fn


def _tmp(name):
    d = tempfile.mkdtemp(prefix="xihe-selftest-%s-" % name)
    _TMPDIRS.append(d)
    return d


def make_cfg(**over):
    """临时目录 + 极短超时的配置，让每条用例都在 1 秒内跑完。"""
    cfg = copy.deepcopy(xihe_config.load())
    root = _tmp("cfg")
    cfg["printer"]["backend"] = "mock"
    cfg["print"].update({"poll_sec": 0.01, "drain_poll": 0.01, "drain_wait": 2.0,
                         "ready_wait": 3.0, "confirm_grace": 0.02,
                         "max_resubmit": 1, "job_appear_wait": 0.6})
    cfg["paths"] = {
        "staging": os.path.join(root, "staging"),
        "logs": os.path.join(root, "logs"),
        "repo": os.path.join(root, "repo"),
        "run": os.path.join(root, "run"),
    }
    for k, v in (over or {}).items():
        if isinstance(v, dict):
            cfg.setdefault(k, {}).update(v)
        else:
            cfg[k] = v
    xihe_config.ensure_dirs(cfg)
    return cfg


def sample_file(name="样例.txt", text="xihe-lite 自检样例\n"):
    d = _tmp("file")
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def make_driver(cfg, backend):
    st = Staging(cfg)
    st.ensure()
    log = _null_log()
    return PrintDriver(cfg, backend=backend, staging=st, log=log), st


class _null_log:
    """自检期间不写日志文件，也不打印。"""
    def info(self, *a, **k):
        pass
    warning = error = critical = info


# ---------------------------------------------------------------- 用例
@case
def t01_config_defaults(v):
    cfg = make_cfg()
    need = ["meta", "console", "paths", "printer", "print", "daemon", "services"]
    miss = [k for k in need if k not in cfg]
    assert not miss, "缺配置段：%s" % miss
    assert isinstance(cfg["console"]["port"], int), "console.port 不是整数"
    example = os.path.join(HERE, "config.example.json")
    assert os.path.isfile(example), "缺 config.example.json"
    with open(example, encoding="utf-8") as f:
        raw = json.load(f)
    for k, val in (raw.get("paths") or {}).items():
        if k.startswith("_"):
            continue
        assert not os.path.isabs(val), "config.example.json 里 %s 不是相对路径：%s" % (k, val)
    return "配置段齐全、模板路径全为相对路径"


@case
def t02_dirs_created(v):
    cfg = make_cfg()
    for key in ("staging", "logs", "repo", "run"):
        p = xihe_config.path(cfg, "paths", key)
        assert os.path.isdir(p), "目录没建出来：%s" % key
    return "staging/logs/repo/run 四个目录自动创建"


@case
def t03_staging_add(v):
    cfg = make_cfg()
    st = Staging(cfg)
    st.ensure()
    src = sample_file()
    queued, err = st.add(src, {"sided": "simplex"})
    assert not err and queued, "入队失败：%s" % err
    assert "__" in queued, "队列名没有时间戳分隔：%s" % queued
    files = st.ready_files()
    assert len(files) == 1, "ready 里应只有 1 个文件，实际 %d" % len(files)
    meta = st.read_meta(files[0])
    assert meta.get("orig") == "样例.txt", "sidecar 丢了原始文件名：%s" % meta
    assert st.counts()["ready"] == 1
    return "入队 → ready=1，sidecar.orig=样例.txt"


@case
def t04_internal_files_skipped(v):
    cfg = make_cfg()
    st = Staging(cfg)
    st.ensure()
    st.add(sample_file())
    # 取消信号 + sidecar 都不能被当成待打印任务
    with open(st.clear_signal, "w", encoding="utf-8") as f:
        json.dump({"ts": time.time()}, f)
    with open(os.path.join(st.root, "x.meta.json"), "w", encoding="utf-8") as f:
        f.write("{}")
    names = [os.path.basename(p) for p in st.ready_files()]
    assert len(names) == 1, "ready 里混进了内部文件：%s" % names
    assert "_clear_signal.json" not in names
    return "取消信号 / .meta.json 不会进 ready 队列"


@case
def t05_mock_chain(v):
    cfg = make_cfg()
    drv, st = make_driver(cfg, B.SimulatedBackend())
    queued, _ = st.add(sample_file())
    job = drv.process(os.path.join(st.root, queued))
    assert job["result"] == states.RESULT_SIMULATED, "演练结果应为 simulated：%s" % job["result"]
    assert job["ok"] and not job["verified"], "演练不应标记为真机确认"
    assert "_模拟" in job["dest"], "演练归档应进 _模拟：%s" % job["dest"]
    assert st.counts()["done"] == 0, "演练文件不许进 _已打印"
    assert st.read_last().get("result") == states.RESULT_SIMULATED, "last_job.json 没写"
    assert "模拟" in job["receipt"], "回执没有中文模拟说明：%s" % job["receipt"]
    return "mock：simulated → _模拟，_已打印 保持 0，回执含中文提示"


@case
def t06_printed_archives_done(v):
    cfg = make_cfg()
    drv, st = make_driver(cfg, B.ScriptedBackend())
    queued, _ = st.add(sample_file())
    job = drv.process(os.path.join(st.root, queued))
    assert job["result"] == states.RESULT_PRINTED, "应判 printed：%s（%s）" % (
        job["result"], job.get("reason"))
    assert job["verified"] is True and "_已打印" in job["dest"]
    assert st.counts()["done"] == 1 and st.counts()["sim"] == 0
    return "printed → _已打印（且 verified=True）"


@case
def t07_jam_failed(v):
    frames = [{"jobs": 1, "status": 0x0008, "cat": "jam"}]
    cfg = make_cfg()
    drv, st = make_driver(cfg, B.ScriptedBackend(frames))
    queued, _ = st.add(sample_file())
    job = drv.process(os.path.join(st.root, queued))
    assert job["result"] == states.RESULT_FAILED, "卡纸应判 failed：%s" % job["result"]
    assert "_失败" in job["dest"], "失败应进 _失败：%s" % job["dest"]
    err = job["dest"] + ".err.txt"
    assert os.path.isfile(err), "失败没留 .err.txt"
    body = open(err, encoding="utf-8").read()
    assert "卡纸" in body, "err.txt 没写清原因：%s" % body[:120]
    assert st.counts()["done"] == 0
    return "EWS=jam → failed + _失败/.err.txt（写明卡纸）"


@case
def t08_paper_out_is_soft(v):
    # 帧序列的语义：第 1 帧被「等任务进队列」消费，之后每帧是一次出纸轮询采样
    frames = [{"jobs": 1, "status": 0x0010, "cat": "processing"},
              {"jobs": 1, "status": 0x0010, "cat": "processing"},
              {"jobs": 0, "status": 0x0010, "cat": "ready"},
              {"jobs": 0, "status": 0x0010, "cat": "ready"}]
    cfg = make_cfg()
    drv, st = make_driver(cfg, B.ScriptedBackend(frames))
    queued, _ = st.add(sample_file())
    job = drv.process(os.path.join(st.root, queued))
    assert job["result"] == states.RESULT_PRINTED, (
        "缺纸位只算软告警，应判 printed：%s（%s）" % (job["result"], job.get("reason")))
    return "PAPER_OUT(0x10) + EWS=ready → printed（缺纸不判失败）"


@case
def t09_ews_overrides_windows(v):
    # Windows/Job 报 ERROR，但 EWS 说 ready → 不许判失败，最后只能 submitted
    frames = [{"jobs": 1, "status": 0x0002, "cat": "ready"}]
    cfg = make_cfg()
    cfg["print"]["drain_wait"] = 0.4
    drv, st = make_driver(cfg, B.ScriptedBackend(frames))
    queued, _ = st.add(sample_file())
    job = drv.process(os.path.join(st.root, queued))
    assert job["result"] == states.RESULT_SUBMITTED, (
        "EWS 权威可用时不许 failed：%s（%s）" % (job["result"], job.get("reason")))
    assert "_队列" in job["dest"], "未确认应进 _队列：%s" % job["dest"]
    assert job["ok"] is False
    return "EWS=ready 压过 Windows 0x2 → submitted → _队列（不误判失败）"


@case
def t10_never_busy_is_not_success(v):
    # 第 1 帧：任务出现在队列；第 2 帧：队列立刻清空，但从未进入打印态
    frames = [{"jobs": 1, "status": 0, "cat": "ready"},
              {"jobs": 0, "status": 0, "cat": "ready"}]
    cfg = make_cfg()
    drv, st = make_driver(cfg, B.ScriptedBackend(frames))
    queued, _ = st.add(sample_file())
    job = drv.process(os.path.join(st.root, queued))
    assert job["result"] == states.RESULT_SUBMITTED, (
        "没见过 processing 不许判成功：%s" % job["result"])
    assert "从未进入打印态" in (job.get("reason") or ""), job.get("reason")
    return "队列清空但从未 processing → submitted（不假成功）"


@case
def t11_clear_cancels(v):
    cfg = make_cfg()
    drv, st = make_driver(cfg, B.ScriptedBackend())
    queued, _ = st.add(sample_file())
    st.write_clear_signal({"ts": time.time(), "by": "selftest", "cleared": 1,
                           "jobs": [], "docs": [queued], "stage": "done"})
    job = drv.process(os.path.join(st.root, queued))
    assert job["result"] == states.RESULT_CANCELLED, "clear 后应判 cancelled：%s" % job["result"]
    assert "_失败" in job["dest"], "取消也应进 _失败：%s" % job["dest"]
    assert st.counts()["done"] == 0, "取消的文件绝不进 _已打印"
    assert "已取消" in job["receipt"]
    return "clear 信号命中 → cancelled → _失败（不进 _已打印）"


@case
def t12_result_mapping(v):
    need = {states.RESULT_PRINTED, states.RESULT_SIMULATED, states.RESULT_SUBMITTED,
            states.RESULT_FAILED, states.RESULT_CANCELLED}
    assert need <= set(states.RESULT_DIR), "结果映射表缺项：%s" % (need - set(states.RESULT_DIR))
    assert states.RESULT_DIR[states.RESULT_PRINTED] == "done"
    assert states.RESULT_DIR[states.RESULT_SIMULATED] == "sim"
    assert states.RESULT_DIR[states.RESULT_SUBMITTED] == "hold"
    assert states.RESULT_DIR[states.RESULT_FAILED] == "failed"
    assert states.RESULT_DIR[states.RESULT_CANCELLED] == "failed"
    return "5 种结果 → done/sim/hold/failed/failed 一一对应"


@case
def t13_retry_from_failed(v):
    cfg = make_cfg()
    frames = [{"jobs": 1, "status": 0x0008, "cat": "jam"}]
    drv, st = make_driver(cfg, B.ScriptedBackend(frames))
    queued, _ = st.add(sample_file())
    job = drv.process(os.path.join(st.root, queued))
    name = os.path.basename(job["dest"])
    back, err = st.retry(name)
    assert not err and back, "重投失败：%s" % err
    assert st.counts()["ready"] == 1, "重投后 ready 应为 1"
    return "从 _失败 重投 → 回到 ready 队列"


@case
def t14_preflight_one_line(v):
    cfg = make_cfg()
    data = PF.run(cfg)
    concl = data["conclusion"]
    assert "\n" not in concl, "结论必须是单行"
    assert data["code"] in (0, 2), "模拟模式的就绪码应为 0 或 2：%s" % data["code"]
    assert "就绪" in concl or "可用" in concl, "结论里没有中文就绪判定：%s" % concl
    assert data["mode"] == "mock"
    return "一行结论（code=%s）：%s" % (data["code"], concl[:60] + "…")


@case
def t15_service_registry(v):
    cfg = make_cfg()
    from xihe.console.services import ServiceManager
    mgr = ServiceManager(cfg)
    st = mgr.status()
    assert len(st) == 1, "默认应只有 1 个受管服务，实际 %d" % len(st)
    s = st[0]
    for k in ("id", "name", "port", "running", "ok", "log"):
        assert k in s, "服务状态缺字段 %s：%s" % (k, s)
    assert s["port"] == int(cfg["daemon"]["port"]), "守护端口与配置不一致"
    assert isinstance(s["running"], bool) and isinstance(s["ok"], bool), "判活值应是布尔"
    return "默认 1 个服务（printd:%s），判活字段齐全（当前 running=%s）" % (
        s["port"], s["running"])


@case
def t16_console_http(v):
    """真起一个控制台实例，打 /api/health、/api/status、/api/print/submit。"""
    from xihe.console import server as cserver

    cfg = make_cfg()
    cserver.build(cfg, log=None)
    port = _free_port()
    srv = cserver.QuietServer(("127.0.0.1", port), cserver.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    try:
        with urllib.request.urlopen(base + "/api/health", timeout=5) as r:
            health = json.loads(r.read().decode("utf-8"))
        assert health["ok"] and health["app"] == "xihe-lite", "健康检查异常：%s" % health

        with urllib.request.urlopen(base + "/api/status", timeout=10) as r:
            st = json.loads(r.read().decode("utf-8"))
        assert st["ok"] and isinstance(st["services"], list), "/api/status 结构不对"
        assert st["print"]["backend"]["name"], "/api/status 没带后端信息"

        src = sample_file("控制台提交.txt")
        body = json.dumps({"path": src}).encode("utf-8")
        req = urllib.request.Request(base + "/api/print/submit", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            sub = json.loads(r.read().decode("utf-8"))
        assert sub["ok"] and sub.get("queued"), "提交打印失败：%s" % sub

        # 测试指令 → 四级路由（Router Skeleton 的 HTTP 面）
        body = json.dumps({"text": "控制台冒烟：下发一条测试指令"}).encode("utf-8")
        req = urllib.request.Request(base + "/api/route/test", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            rt = json.loads(r.read().decode("utf-8"))
        assert rt["ok"] and rt["tier"], "路由接口未命中：%s" % rt
        assert any("[ROUTE]" in x for x in rt["route_log"]), "路由接口没回 [ROUTE] 行"
        return "控制台 :%d 起来后 health/status/submit/route 全通（入队 %s，路由 %s）" % (
            port, sub["queued"], rt["tier"])
    finally:
        srv.shutdown()
        srv.server_close()


@case
def t17_web_assets(v):
    web = os.path.join(HERE, "xihe", "console", "web", "index.html")
    assert os.path.isfile(web), "缺状态看板页面 xihe/console/web/index.html"
    html = open(web, encoding="utf-8").read()
    for kw in ("打印调度", "前置自检", "/api/status", "/api/print/submit",
               "/api/preflight", "暂存"):
        assert kw in html, "看板页面缺关键内容：%s" % kw
    assert len(html) > 4000, "看板页面太小，可能被截断"
    return "看板页面就位（%.1f KB，含打印调度/自检/状态轮询）" % (len(html) / 1024.0)


@case
def t18_compile_all(v):
    import py_compile
    n = 0
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in ("runtime", "__pycache__", ".git")]
        for f in files:
            if f.endswith(".py"):
                py_compile.compile(os.path.join(root, f), doraise=True)
                n += 1
    assert n >= 10, "编译的 .py 太少：%d" % n
    return "%d 个 .py 全部 py_compile 通过" % n


@case
def t19_privacy_clean(v):
    import privacy_scan
    r = privacy_scan.scan(HERE)
    assert r["ok"], "隐私扫描命中 %d 处：%s" % (
        len(r["hits"]), [(h["file"], h["rule"]) for h in r["hits"][:5]])
    return "隐私扫描 %d 个文件 / %d 条规则 → 零命中" % (r["files"], r["rules"])


@case
def t20_converter_images(v):
    """图片 → A4 PDF（这条路径真机打印才用到，这里直接验转换本身）。"""
    from xihe.printing import converter
    cfg = make_cfg()
    try:
        from PIL import Image
    except Exception:
        return "跳过：未安装 Pillow（真机图片打印才需要）"
    d = _tmp("img")
    paths = []
    for i, color in enumerate(("white", "white"), 1):
        p = os.path.join(d, "p%d.png" % i)
        Image.new("RGB", (600, 400), color).save(p)
        paths.append(p)
    pdf, err = converter.images_to_pdf(paths, cfg, per_page=1)
    assert pdf and os.path.isfile(pdf), "图片合成 PDF 失败：%s" % err
    assert os.path.getsize(pdf) > 1000, "生成的 PDF 太小：%s" % os.path.getsize(pdf)
    n = converter.page_count(pdf)
    assert n in (0, 2), "页数异常：%s" % n          # 没装 pypdfium2 时为 0
    _, err2 = converter.office_to_pdf(os.path.join(d, "p1.png"), cfg)
    assert err2 and "不是 Office 文件" in err2, "非 Office 文件应返回中文原因"
    return "2 张图 → 一个多页 A4 PDF（%.1f KB，页数=%s）" % (os.path.getsize(pdf) / 1024.0, n)


@case
def t21_auto_degrades(v):
    """打印机不可用时，auto 必须降级 mock 并给出中文原因。"""
    cfg = make_cfg()
    cfg["printer"]["backend"] = "auto"
    cfg["printer"]["name"] = "__xihe_no_such_printer__"
    be, line = B.make_backend(cfg)
    assert be.simulated, "打印机不可用时 auto 必须降级 mock：%s" % line
    assert ("降级" in line) or ("模拟" in line), "降级说明不是中文人话：%s" % line
    return "auto + 不存在的打印机 → mock：%s" % line[:56]


@case
def t22_router_fallback(v):
    """四级路由骨架：默认配置必须从云端降级到本地兜底，并写出 [ROUTE] 日志。"""
    from xihe import router
    cfg = make_cfg()
    res = router.route("自检：请打印一张测试页", cfg)
    assert res["ok"], "路由应命中某一级：%s" % res["attempts"]
    assert res["tier"] == "local_llm", "默认应落到本地兜底，实际 %s" % res["tier"]
    assert any("[ROUTE]" in x for x in res["route_log"]), "route_log 里没有 [ROUTE] 行"
    assert any("切换至本地兜底" in x for x in res["route_log"]), (
        "缺少「切换至本地兜底」流转：%s" % res["route_log"])
    logf = os.path.join(xihe_config.path(cfg, "paths", "logs"), "router.log")
    assert os.path.isfile(logf), "没有写出 router.log"
    body = open(logf, encoding="utf-8").read()
    assert "[ROUTE]" in body and "切换至本地兜底" in body, "router.log 内容不含流转记录"
    return "云端不可用 → 本地兜底命中（%d ms），router.log 有 [ROUTE] 流转" % res["elapsed_ms"]


@case
def t23_router_offline_rule(v):
    """四级全关时，必须落到离线规则（保证演示不空转）。"""
    from xihe import router
    cfg = make_cfg()
    cfg["router"]["mock_available"].update({"cloud_api": False, "local_llm": False,
                                            "remote_compute": False})
    res = router.route("全部通道都关掉试试", cfg)
    assert res["ok"] and res["tier"] == "offline_rule", "应落到离线规则：%s" % res["tier"]
    assert any("切换至离线规则" in x for x in res["route_log"]), res["route_log"]
    return "三级都不可用 → 离线规则兜底（永不失败）"


def _free_port():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


# ---------------------------------------------------------------- 主流程
def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="xihe-lite 自检（离线，不打印）")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    t0 = time.time()
    results = []
    for fn in CASES:
        title = fn.__doc__ or fn.__name__
        name = fn.__name__
        try:
            detail = fn(a.verbose) or ""
            results.append({"case": name, "ok": True, "detail": detail})
            if not a.json:
                print("PASS  %-34s %s" % (name, detail))
        except Exception as e:
            tb = traceback.format_exc(limit=3)
            results.append({"case": name, "ok": False,
                            "detail": "%s: %s" % (type(e).__name__, e), "tb": tb})
            if not a.json:
                print("FAIL  %-34s %s: %s" % (name, type(e).__name__, e))
                if a.verbose:
                    print(tb)
    for d in _TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)

    passed = sum(1 for r in results if r["ok"])
    total = len(results)
    summary = "SUMMARY: %d/%d passed" % (passed, total)
    if a.json:
        print(json.dumps({"ok": passed == total, "passed": passed, "total": total,
                          "seconds": round(time.time() - t0, 2),
                          "cases": results}, ensure_ascii=False, indent=2))
    else:
        print("")
        print("%s（%.2fs，全程未接触任何打印机）" % (summary, time.time() - t0))
        if passed != total:
            print("失败用例：%s" % ", ".join(r["case"] for r in results if not r["ok"]))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
