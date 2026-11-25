import json
import os
import threading
import time
import uuid
from pathlib import Path

import requests
from apscheduler.triggers.cron import CronTrigger
from lxml import etree

from app.core.config import settings
from app.sdk import scheduler
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from .core import EXT, atomic, digest, identify, missing, update


class LocalNfoRefresh(_PluginBase):
    plugin_name = "本地 NFO 标题与简介更新"
    plugin_desc = "预览、修复或覆盖电影和分集标题与简介，定时复查缺失资料。"
    plugin_icon = "https://raw.githubusercontent.com/mapeili521/MoviePilot-Plugins/main/icons/LocalNfoRefresh.png"
    plugin_version = "0.1.1"
    plugin_author = "mapeili521"
    author_url = "https://github.com/mapeili521"
    plugin_config_prefix = "localnforefresh_"
    plugin_order = 50
    auth_level = 1

    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self.cfg = {}
        self.generation = 0

    def init_plugin(self, config=None):
        self.stop_service()
        self.cfg = dict(config or {})
        action = self.cfg.pop("action", "")
        if action:
            self.update_config(dict(self.cfg, action=""))
            scheduler.add_plugin_once_job(type(self).__name__, "manual", self.run, "NFO 手动任务",
                                          delay_seconds=2, func_kwargs={"action": action})

    def get_state(self):
        return bool(self.cfg.get("enabled"))

    def get_service(self):
        if not self.get_state():
            return []
        trigger = CronTrigger.from_crontab(self.cfg.get("cron") or "30 4 * * *",
                                           timezone="Asia/Shanghai")
        return [dict(id="LocalNfoRefresh.daily", name="复查本地元数据", trigger=trigger,
                     func=self.run, kwargs={})]

    def stop_service(self):
        self.generation += 1
        scheduler.remove_plugin_once_job(type(self).__name__, "manual")

    def get_api(self):
        return []

    def get_command(self):
        return []

    def run(self, action="定时复查"):
        if not self.lock.acquire(blocking=False):
            return
        rows = []
        try:
            cfg = dict(self.cfg)
            roots = [Path(s.strip()) for s in cfg.get("roots", "").splitlines() if s.strip()]
            if not roots or any(not r.is_absolute() or not r.is_dir() for r in roots):
                raise ValueError("请填写 MP 容器内存在的绝对目录，每行一个")
            roots = [r.resolve() for r in roots]
            if action == "撤销最近批次":
                self.rollback(roots, rows)
            else:
                self.scan(cfg, roots, action, rows)
        except Exception as exc:
            rows.append("任务中止：" + str(exc))
        finally:
            try:
                self.save_data("report", rows[-300:] or ["本次没有到期条目"])
                logger.info("本地 NFO 更新：%s", (rows or ["无到期条目"])[-1])
                if self.cfg.get("notify") and rows:
                    self.post_message(title=self.plugin_name, text=rows[-1])
            finally:
                self.lock.release()

    def scan(self, cfg, roots, action, rows):
        generation = self.generation
        preview = action == "仅预览" or (action == "定时复查" and not cfg.get("write"))
        state = self.get_data("queue") or {}
        mappings = json.loads(cfg.get("mapping") or "{}")
        limit = max(1, min(1000, int(cfg.get("limit") or 100)))
        now = time.time()
        candidates = {}
        for root in roots:
            for directory, _, files in os.walk(root):
                for name in files:
                    path = Path(directory) / name
                    if path.suffix.lower() in EXT and not path.is_symlink():
                        candidates[str(path)] = root
        pending = sorted(candidates, key=lambda p: (state.get(p, {}).get("due", 0), p))
        cache = {}
        batch = uuid.uuid4().hex
        modes = {f: cfg.get(f, "修复占位") for f in ("title", "plot")}
        changed = waiting = errors = total = 0
        for key in pending:
            previous = state.get(key, {})
            if generation != self.generation or total >= limit:
                break
            if action == "定时复查" and previous.get("due", 0) > now:
                continue
            total += 1
            delay = 86400
            try:
                path, old, root, endpoint = identify(Path(key), candidates[key], cfg.get("create"), mappings)
                if endpoint not in cache:
                    cache[endpoint] = self.lookup(endpoint, cfg)
                changes = update(root, cache[endpoint], modes)
                unknown = any(modes[f] != "保留" and missing(root.findtext(f) or "") for f in modes)
                if changes:
                    if not preview and generation == self.generation:
                        data = etree.tostring(root.getroottree(), encoding="UTF-8", xml_declaration=True)
                        self.backed_write(path, old, data, batch)
                        item_id = str(mappings.get(key, {}).get("emby_id", ""))
                        if cfg.get("emby_url") and cfg.get("emby_key") and item_id.isdecimal():
                            try:
                                response = requests.post(
                                    cfg["emby_url"].rstrip("/") + "/Items/" + item_id + "/Refresh",
                                    headers={"X-Emby-Token": cfg["emby_key"]},
                                    params={"Recursive":"false", "MetadataRefreshMode":"Default",
                                            "ReplaceAllMetadata":"false", "ReplaceAllImages":"false"},
                                    timeout=(5, 15), allow_redirects=False)
                                rows.append(f"Emby 条目 {item_id} 刷新请求 HTTP {response.status_code}")
                            except requests.RequestException:
                                rows.append("Emby 刷新请求失败，NFO 已写入")
                    rows.append(key + " " + json.dumps(changes, ensure_ascii=False))
                    changed += 1
                if unknown:
                    waiting += 1
                    delay = min(7 * 86400, max(86400, previous.get("delay", 0) * 2))
                else:
                    delay = 7 * 86400
            except requests.HTTPError as exc:
                code = exc.response.status_code
                rows.append(f"{key}：TMDB HTTP {code}")
                errors += 1
                if code in (401, 403, 429) or code >= 500:
                    break
            except requests.RequestException:
                rows.append("TMDB 网络异常，下次再试")
                errors += 1
                break
            except (ValueError, TypeError, OSError, etree.XMLSyntaxError) as exc:
                errors += 1
                rows.append(f"{key}：{exc}")
            if not preview or action == "定时复查":
                state[key] = dict(due=now + delay, delay=delay)
        if not preview or action == "定时复查":
            self.save_data("queue", {k:v for k,v in state.items() if k in candidates})
        rows.append(f"{'预览' if preview else '执行'}：检查 {total}，差异 {changed}，待补 {waiting}，异常 {errors}")

    def lookup(self, endpoint, cfg):
        token = cfg.get("token") or getattr(settings, "TMDB_API_KEY", "")
        if hasattr(token, "get_secret_value"):
            token = token.get_secret_value()
        if not token:
            raise ValueError("请配置 TMDB API Key 或 Read Access Token")
        result = {"title": "", "plot": ""}
        for lang in dict.fromkeys(["zh-CN", cfg.get("fallback") or "zh-CN"]):
            params = {"language": lang}
            headers = {}
            if len(token) == 32:
                params["api_key"] = token
            else:
                headers["Authorization"] = "Bearer " + token
            resp = requests.get("https://api.themoviedb.org/3/" + endpoint, params=params,
                                headers=headers, timeout=(5, 15))
            resp.raise_for_status()
            obj = resp.json()
            for f, val in (("title", obj.get("name") or obj.get("title")), ("plot", obj.get("overview"))):
                if not result[f] and val and not missing(val):
                    result[f] = val
            if all(result.values()):
                break
        return result

    def backed_write(self, path, old, data, batch):
        directory = Path(self.get_data_path()) / "backups" / batch
        directory.mkdir(parents=True, exist_ok=True)
        key = uuid.uuid4().hex
        if old is not None:
            (directory / (key + ".bak")).write_bytes(old)
        journal = dict(path=str(path), after=digest(data), existed=old is not None)
        (directory / (key + ".json")).write_text(json.dumps(journal), encoding="utf-8")
        self.save_data("last_batch", batch)
        atomic(path, data, old)

    def rollback(self, roots, rows):
        batch = self.get_data("last_batch")
        if not batch:
            rows.append("没有可撤销的批次")
            return
        directory = Path(self.get_data_path()) / "backups" / batch
        for journal in directory.glob("*.json"):
            item = json.loads(journal.read_text())
            path = Path(item["path"])
            if path.is_symlink() or not any(path.resolve().is_relative_to(r) for r in roots):
                rows.append("路径已变化：" + str(path))
                continue
            current = path.read_bytes() if path.is_file() else None
            if current is None or digest(current) != item["after"]:
                rows.append("跳过已变化文件：" + str(path))
                continue
            if item["existed"]:
                atomic(path, journal.with_suffix(".bak").read_bytes(), current)
            else:
                path.unlink()
            journal.unlink()
            rows.append("已撤销：" + str(path))

    def get_page(self):
        return [{"component":"pre", "props":{"style":"white-space:pre-wrap;overflow-wrap:anywhere"},
                 "text":"\n".join(self.get_data("report") or ["尚无运行记录"])}]

    def get_form(self):
        modes = ["修复占位", "只补空缺", "跟随覆盖", "保留"]
        fields = [
            ("enabled","启用定时任务","VSwitch",{}), ("write","允许定时写入","VSwitch",{}),
            ("roots","媒体根目录（容器内绝对路径，每行一个）","VTextarea",{}),
            ("title","标题策略","VSelect",{"items":modes}), ("plot","简介策略","VSelect",{"items":modes}),
            ("cron","北京时间 Cron","VTextField",{}), ("limit","每轮最多检查文件数","VTextField",{}),
            ("create","允许为可靠匹配创建 NFO","VSwitch",{}),
            ("token","TMDB 凭据（可留空以读取宿主 API Key）","VTextField",{"type":"password"}),
            ("fallback","可选回退语言，如 en-US","VTextField",{}),
            ("mapping","手动映射 JSON，以媒体文件完整路径为键，值含 id、可选 season 和 episode","VTextarea",{}),
            ("emby_url","可选 Emby API 地址（含 /emby）","VTextField",{}),
            ("emby_key","可选 Emby API Key，配合映射中的 emby_id","VTextField",{"type":"password"}),
            ("notify","通过 MP 发送运行摘要","VSwitch",{}),
            ("action","保存后执行一次","VSelect",{"items":["","仅预览","立即写入","撤销最近批次"]})]
        nodes = [{"component":comp,"props":dict(props, model=name, label=label)} for name,label,comp,props in fields]
        return [{"component":"VForm","content":nodes}], dict(enabled=False, write=False, title=modes[0],
                plot=modes[0], cron="30 4 * * *", limit=100, create=False, notify=False)
