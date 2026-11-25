import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins.v3/localnforefresh"))
from core import atomic, identify, update


class EpisodeRefresh(unittest.TestCase):
    def test_field_merge_and_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "tvshow.nfo").write_text("<tvshow><tmdbid>100</tmdbid></tvshow>")
            media = folder / "示例.S01E19.strm"
            sidecar = media.with_suffix(".nfo")
            sidecar.write_text("<episodedetails><title>第19集</title><plot>旧简介</plot>"
                               "<rating>8</rating></episodedetails>")
            path, original, root, endpoint = identify(media, folder, False, {})
            self.assertEqual(endpoint, "tv/100/season/1/episode/19")
            remote = {"title":"归途", "plot":""}
            modes = {"title":"修复占位", "plot":"跟随覆盖"}
            self.assertEqual(update(root, remote, modes), {"title":["第19集","归途"]})
            self.assertEqual(root.findtext("plot"), "旧简介")
            self.assertEqual(root.findtext("rating"), "8")
            self.assertEqual(update(root, remote, modes), {})
            path.write_bytes(b"external edit")
            with self.assertRaises(ValueError):
                atomic(path, b"replacement", original)
            self.assertEqual(path.read_bytes(), b"external edit")
