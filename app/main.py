# -*- coding: utf-8 -*-
"""主编排器：news | scan | quant | digest | all | auto"""
import argparse
import os
import traceback

from . import backtest, factors, news, notifier, options, review, utils
from .ai import DeepSeekClient

STATUS_FILE = utils.DATA_DIR + "/status.json"


def load_cfg(name: str) -> dict:
    return utils.load_json(os.path.join(utils.CONFIG_DIR, f"{name}.json"), default={})


def _update_status(mode: str, result: dict):
    st = utils.load_json(STATUS_FILE, default={"last_runs": {}, "errors": []})
    st["updated_at"] = utils.bj_now_str()
    st["last_runs"][mode] = {
        "at": utils.bj_now_str(),
        "ok": bool(result.get("ok")),
        "detail": {k: v for k, v in result.items() if k not in ("new_hits",)},
    }
    if not result.get("ok") and result.get("error"):
        st["errors"] = ([{"at": utils.bj_now_str(), "mode": mode, "msg": result["error"]}] + st["errors"])[:10]
    utils.save_json(STATUS_FILE, st)


def mode_news(app_cfg, client, force_ai=False):
    try:
        r = news.run_news(app_cfg, client, force_ai=force_ai)
        _update_status("news", r)
        return r
    except Exception as e:  # noqa: BLE001
        _update_status("news", {"ok": False, "error": f"{e}"})
        traceback.print_exc()
        return {"ok": False, "error": str(e)}


def mode_scan(o_cfg, app_cfg, allow_email=False):
    try:
        r = options.scan_options(o_cfg)
        r["email"] = ""
        if allow_email and r.get("new_hits"):
            ok, err = notifier.send_hits_alert(r["new_hits"], o_cfg, app_cfg.get("site_url", ""))
            r["email"] = f"sent:{len(r['new_hits'])}" if ok else f"skip:{err}"
        _update_status("scan", r)
        return r
    except Exception as e:  # noqa: BLE001
        _update_status("scan", {"ok": False, "error": f"{e}"})
        traceback.print_exc()
        return {"ok": False, "error": str(e)}


def mode_quant(bt_cfg, f_cfg, watchlist, client, app_cfg, force_ai=False):
    try:
        r1 = backtest.run_backtests(bt_cfg, watchlist)
        ai_interval = int(app_cfg.get("ai", {}).get("factor_interval_min", 60))
        r2 = factors.run_factors(f_cfg, client, force_ai=force_ai, ai_interval_min=ai_interval)
        r = {"ok": bool(r1.get("ok") and r2.get("ok")), "backtest": r1, "factors": r2}
        _update_status("quant", r)
        return r
    except Exception as e:  # noqa: BLE001
        _update_status("quant", {"ok": False, "error": f"{e}"})
        traceback.print_exc()
        return {"ok": False, "error": str(e)}


def mode_digest(app_cfg):
    try:
        ok, err = notifier.send_digest(app_cfg)
        r = {"ok": ok, "error": err}
        _update_status("digest", r)
        return r
    except Exception as e:  # noqa: BLE001
        _update_status("digest", {"ok": False, "error": f"{e}"})
        return {"ok": False, "error": str(e)}


def mode_review(app_cfg):
    try:
        r = review.run_review(app_cfg)
        r["email"] = ""
        ok, err = notifier.send_review(r, app_cfg)
        r["email"] = "sent" if ok else f"skip:{err}"
        _update_status("review", r)
        return r
    except Exception as e:  # noqa: BLE001
        _update_status("review", {"ok": False, "error": f"{e}"})
        traceback.print_exc()
        return {"ok": False, "error": str(e)}


def pick_mode(now) -> str:
    """按北京时间分钟数对应定时计划（摘要窗口留 5 分钟余量防调度延迟）。"""
    h, m = now.hour, now.minute
    if h == 23 and m <= 10:
        return "review"
    if h == 22 and 25 <= m <= 35:
        return "digest"
    if m in (5, 35):
        return "scan"
    if m == 20:
        return "quant"
    return "news"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="auto", choices=["auto", "news", "scan", "quant", "digest", "review", "all"])
    ap.add_argument("--allow-email", action="store_true", help="允许发送邮件（Actions 中自动开启）")
    ap.add_argument("--no-ai", action="store_true", help="禁用 AI 调用")
    ap.add_argument("--force-ai", action="store_true", help="忽略时间间隔强制调用 AI")
    args = ap.parse_args()

    if os.environ.get("GITHUB_ACTIONS") == "true":
        args.allow_email = True

    app_cfg = load_cfg("app")
    ai_cfg = app_cfg.get("ai", {})
    client = DeepSeekClient(
        model=ai_cfg.get("model", "deepseek-chat"),
        timeout=int(ai_cfg.get("timeout_sec", 90)),
        max_output_tokens=int(ai_cfg.get("max_output_tokens", 1200)),
    )
    if args.no_ai:
        client.api_key = ""

    print(f"== quant-live 运行开始 [{utils.bj_now_str()}] mode={args.mode} "
          f"ai={'on' if client.available else 'off'} email={'on' if args.allow_email else 'off'} ==")

    mode = pick_mode(utils.bj_now()) if args.mode == "auto" else args.mode
    results = {}
    if mode in ("news", "all"):
        results["news"] = mode_news(app_cfg, client, force_ai=args.force_ai)
    if mode in ("scan", "all"):
        results["scan"] = mode_scan(load_cfg("options"), app_cfg, allow_email=args.allow_email)
    if mode in ("quant", "all"):
        results["quant"] = mode_quant(load_cfg("backtest"), load_cfg("factors"),
                                      load_cfg("watchlist"), client, app_cfg, force_ai=args.force_ai)
    if mode in ("digest", "all"):
        results["digest"] = mode_digest(app_cfg)
    if mode in ("review", "all"):
        results["review"] = mode_review(app_cfg)

    print(f"== quant-live 完成 {results} ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
