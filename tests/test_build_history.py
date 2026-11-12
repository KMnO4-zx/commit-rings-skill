"""Real Git fixtures exercise data integrity; no network or third-party packages."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/commit-rings-skill/scripts/build_history.py"
spec = importlib.util.spec_from_file_location("build_history", SCRIPT)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


class GitFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "source"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Same Name")
        self.git("config", "user.email", "first@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.tick = 0

    def git(self, *args, env=None):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], env=env, stderr=subprocess.DEVNULL).decode().strip()

    def commit(self, message, email=None):
        self.tick += 1
        stamp = f"2024-01-01T00:00:{self.tick:02d}+0000"
        env = dict(os.environ, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        if email:
            env["GIT_AUTHOR_EMAIL"] = email
        self.git("add", "--all")
        self.git("commit", "--allow-empty", "-m", message, env=env)
        return self.git("rev-parse", "HEAD")

    def test_changes_merges_unicode_and_escaping(self):
        unusual = "数据 空格\t换行\n.md"
        (self.repo / unusual).write_text("first")
        first = self.commit("seed </script><script>alert(1)</script> & 雨")
        self.git("checkout", "-b", "feature")
        (self.repo / "feature.py").write_text("x=1")
        feature = self.commit("branch", "second@example.invalid")
        self.git("checkout", "main")
        self.git("mv", unusual, "renamed.md")
        rename = self.commit("rename")
        self.git("merge", "--no-ff", "feature", "-m", "merge")
        merge = self.git("rev-parse", "HEAD")
        (self.repo / "renamed.md").unlink()
        self.commit("delete")
        head = self.commit("empty commit")
        commits, paths, head_files = b.collect_git(self.repo, head)
        self.assertEqual(len(commits), 6)
        self.assertEqual(head_files, 1)
        self.assertIn(unusual, paths)
        by_sha = {c["h"]: c for c in commits}
        self.assertEqual(by_sha[merge]["f"], [])
        self.assertTrue(by_sha[merge]["m"])
        self.assertEqual({(paths[p], action) for p, action in by_sha[rename]["f"]}, {(unusual, "D"), ("renamed.md", "A")})
        self.assertEqual(by_sha[first]["f"][0][1], "A")
        self.assertEqual(by_sha[head]["f"], [])
        raw = copy.deepcopy(commits)
        authors, _, matched = b.group_authors(commits, {})
        self.assertEqual(len(authors), 2, "same display name must not merge different emails")
        self.assertEqual(matched, 0)
        self.assertEqual(sum(a["total"] for a in authors), len(commits))
        b.classify(commits, paths)
        data = dict(repo="fixture/example", head=head, branch="main", commits=commits, paths=paths, authors=authors,
                    categories=b.CATEGORIES, identityCoverage={"queried": 0, "matched": 0, "total": 6})
        rendered = b.render(data)
        self.assertNotIn("</script><script>alert", rendered)
        self.assertNotIn("first@example.invalid", rendered)
        self.assertNotIn("second@example.invalid", rendered)
        embedded = re.search(r'<script id="history-data" type="application/json">(.*?)</script>', rendered, re.S)[1]
        self.assertEqual(json.loads(embedded), data)
        mapping = {first: {"id": 42, "login": "real-login"}, feature: {"id": 42, "login": "real-login"}}
        grouped, _, matched = b.group_authors(raw, mapping)
        self.assertEqual(matched, 2)
        self.assertEqual(next(a for a in grouped if a["github_id"] == 42)["total"], 2)

    def test_one_commit_cli_and_read_only_local_source(self):
        (self.repo / "README.md").write_text("one")
        self.commit("first")
        before = self.git("status", "--porcelain")
        output = Path(self.tmp.name) / "result.html"
        result = subprocess.run(["python3", str(SCRIPT), "fixture/one", "--local-repo", str(self.repo),
                                 "--offline", "--cache-dir", str(Path(self.tmp.name) / "cache"), "-o", str(output)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["commits"], 1)
        self.assertTrue(output.exists())
        self.assertEqual(before, self.git("status", "--porcelain"))

    def test_shallow_history_is_rejected(self):
        self.commit("one")
        self.commit("two")
        shallow = Path(self.tmp.name) / "shallow"
        subprocess.run(["git", "clone", "--depth=1", self.repo.as_uri(), str(shallow)], check=True, capture_output=True)
        with self.assertRaisesRegex(b.BuildError, "Shallow"):
            b.source_repo("fixture/shallow", Path(self.tmp.name) / "cache", local=shallow)

    def test_empty_repo_fails_without_artifact(self):
        with self.assertRaises(b.BuildError):
            b.source_repo("fixture/empty", Path(self.tmp.name) / "cache", local=self.repo)


class InputAndAPI(unittest.TestCase):
    def test_repo_input(self):
        self.assertEqual(b.normalize_repo("https://github.com/owner/repo.git/"), "owner/repo")
        for invalid in ("../repo", "owner/..", "owner/repo/tree/main", "--upload-pack=oops", "https://example.com/a/b"):
            with self.assertRaises(b.BuildError):
                b.normalize_repo(invalid)

    def test_pagination_null_authors_and_cache(self):
        class Response:
            def __init__(self, rows): self.rows = rows
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(self.rows).encode()
        pages = [[{"sha": str(i), "author": {"id": 1, "login": "one"}} for i in range(100)], [{"sha": "last", "author": None}]]
        with tempfile.TemporaryDirectory() as tmp, patch.object(b, "auth_token", return_value=None), patch.object(b.urllib.request, "urlopen", side_effect=[Response(p) for p in pages]) as fetch:
            cache = Path(tmp)
            mapping = b.fetch_authors("fixture/repo", "a" * 40, cache)
            self.assertEqual(len(mapping), 101)
            self.assertIsNone(mapping["last"])
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(b.fetch_authors("fixture/repo", "a" * 40, cache, offline=True), mapping)
            self.assertEqual(fetch.call_count, 2)

    def test_rate_limit_keeps_real_git_data_available(self):
        error = b.urllib.error.HTTPError("https://api.github.com", 403, "rate limited", {}, None)
        with tempfile.TemporaryDirectory() as tmp, patch.object(b, "auth_token", return_value=None), patch.object(b.urllib.request, "urlopen", side_effect=error):
            self.assertEqual(b.fetch_authors("fixture/repo", "a" * 40, Path(tmp)), {})


if __name__ == "__main__":
    unittest.main()
