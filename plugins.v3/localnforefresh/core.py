"""Local sidecar inspection and conservative XML updates."""
import hashlib
import os
import re
import tempfile
from pathlib import Path

from lxml import etree


def digest(data):
    return hashlib.sha256(data).hexdigest()


def parse(data):
    parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True,
                             remove_blank_text=False, strip_cdata=False)
    root = etree.fromstring(data, parser)
    if root.getroottree().docinfo.doctype:
        raise ValueError("跳过包含 DTD 的 NFO")
    return root


def missing(text):
    return not text.strip() or bool(re.fullmatch(
        r"(?:第\s*[\d零〇一二两三四五六七八九十百]+\s*[集话話]|(?:episode|ep)\.?\s*\d+|暂无简介[。.]?|暂无剧情简介[。.]?)",
        text.strip(), re.I))


def update(root, remote, modes):
    changes = {}
    for field, value in remote.items():
        mode = modes[field]
        nodes = root.findall(field)
        if len(nodes) > 1 or (nodes and len(nodes[0])):
            raise ValueError("字段结构不明确：" + field)
        old = (nodes[0].text or "") if nodes else ""
        value = (value or "").strip()
        if mode == "保留" or missing(value) or value == old:
            continue
        if mode == "只补空缺" and old.strip():
            continue
        if mode == "修复占位" and not missing(old):
            continue
        node = nodes[0] if nodes else etree.SubElement(root, field)
        node.text = value
        changes[field] = [old, value]
    return changes


def atomic(path, data, expected):
    if path.is_symlink():
        raise ValueError("跳过符号链接")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            if expected is None:
                os.chmod(temp, 0o644)
                os.link(temp, path)
            else:
                os.chmod(temp, path.stat().st_mode & 0o777)
                if path.read_bytes() != expected:
                    raise ValueError("文件已变化，跳过")
                os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def identity(root):
    ids = {n.text.strip() for n in root.findall("uniqueid")
           if n.get("type", "").lower() == "tmdb" and n.text}
    ids.update(n.text.strip() for n in root.findall("tmdbid") if n.text)
    if len(ids) > 1:
        raise ValueError("TMDB ID 冲突")
    return next(iter(ids), "")


def identify(media, folder, create, mappings):
    match = re.search(r"(?i)(?<![a-z0-9])S(\d{1,3})E(\d{1,4})(?!\d)", media.stem)
    nfo = media.with_suffix(".nfo")
    if not match and (media.parent / "movie.nfo").exists():
        sibling = media.parent / "movie.nfo"
        if sibling != nfo and nfo.exists():
            raise ValueError("存在多个候选 NFO")
        if len([p for p in media.parent.iterdir() if p.suffix.lower() in EXT]) != 1:
            raise ValueError("多部媒体共用 movie.nfo")
        nfo = sibling
    if nfo.is_symlink():
        raise ValueError("跳过符号链接")
    original = nfo.read_bytes() if nfo.exists() else None
    if original is None and not create:
        raise ValueError("缺少 NFO")
    root = parse(original) if original is not None else etree.Element("episodedetails" if match else "movie")
    override = mappings.get(str(media), {})
    if root.tag == "movie":
        if match:
            raise ValueError("电影与分集身份冲突")
        kind = "movie"
        tmdb = identity(root)
    elif root.tag == "episodedetails":
        if match and re.search(r"(?i)^(?:[ ._-]*E\d+|\s*-\s*\d+)", media.stem[match.end():]) and not override:
            raise ValueError("多集文件需指定映射")
        season = root.findtext("season")
        episode = root.findtext("episode")
        if match:
            if not override and ((season and int(season) != int(match[1])) or
                                 (episode and int(episode) != int(match[2]))):
                raise ValueError("文件名与 NFO 季集号冲突")
            season, episode = match.groups()
        season = int(override.get("season", season))
        episode = int(override.get("episode", episode))
        if season < 0 or episode < 1:
            raise ValueError("季集号无效")
        kind = f"tv/{{id}}/season/{season}/episode/{episode}"
        tmdb = ""
        for parent in media.parents:
            show = parent / "tvshow.nfo"
            if show.is_file() and not show.is_symlink():
                tmdb = identity(parse(show.read_bytes()))
                break
            if parent == folder:
                break
        if original is None:
            etree.SubElement(root, "season").text = str(season)
            etree.SubElement(root, "episode").text = str(episode)
    else:
        raise ValueError("不支持的 NFO 类型")
    tmdb = str(override.get("id", tmdb))
    if not tmdb.isdecimal():
        raise ValueError("缺少可靠的电影或剧集 TMDB ID")
    if original is None and kind == "movie":
        etree.SubElement(root, "uniqueid", type="tmdb").text = tmdb
    endpoint = f"movie/{tmdb}" if kind == "movie" else kind.format(id=tmdb)
    return nfo, original, root, endpoint


EXT = {".strm", ".mkv", ".mp4", ".avi", ".mov", ".ts", ".m2ts", ".iso", ".wmv", ".m4v"}
