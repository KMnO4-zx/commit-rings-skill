#!/usr/bin/env python3
"""Build an offline Git growth-ring HTML. Python 3.10+, Git; no pip dependencies."""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

SKILL_ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = [
    {"label": "源码", "color": "#39b853"},
    {"label": "文档", "color": "#e6bc27"},
    {"label": "测试", "color": "#8fca43"},
    {"label": "构建与配置", "color": "#ed8d28"},
    {"label": "资源与数据", "color": "#f16b37"},
    {"label": "维护与合并", "color": "#217a42"},
]


class BuildError(Exception):
    pass


def note(message):
    print(message, file=sys.stderr, flush=True)


def normalize_repo(value):
    value = value.strip().removesuffix("/")
    if value.startswith("https://github.com/"):
        value = value[len("https://github.com/"):]
    value = value.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", value):
        raise BuildError("Use owner/repo or https://github.com/owner/repo.")
    if value.split("/")[1] in (".", ".."):
        raise BuildError("Invalid repository name.")
    return value


def git(path, *args, input=None, timeout=600):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1",
               GIT_NO_LAZY_FETCH="1", GIT_OPTIONAL_LOCKS="0")
    cmd = ["git", "-c", "core.quotepath=false", "-c", "log.showSignature=false"]
    if path is not None:
        cmd += ["-C", str(path)]
    try:
        result = subprocess.run(cmd + list(args), input=input, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BuildError(f"Git could not finish: {type(exc).__name__}") from exc
    if result.returncode:
        # No credential material is included in arguments or propagated into HTML.
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise BuildError(f"git {args[0]} failed: {detail}")
    return result.stdout


def source_repo(repo, cache, local=None, ref=None, offline=False):
    if ref and (ref.startswith("-") or any(c in ref for c in "\r\n\x00")):
        raise BuildError("Invalid Git ref.")
    if local:
        path = Path(local).expanduser().resolve()
        head = git(path, "rev-parse", "--verify", "--end-of-options", (ref or "HEAD") + "^{commit}").decode().strip()
        try:
            branch = ref or git(path, "symbolic-ref", "--short", "HEAD").decode().strip()
        except BuildError:
            branch = "detached HEAD"
    else:
        path = cache / "repository.git"
        if offline:
            if not path.exists():
                raise BuildError("No cached repository. Use --local-repo or run online first.")
            head = git(path, "rev-parse", "--verify", (ref or "FETCH_HEAD") + "^{commit}").decode().strip()
            meta = read_json(cache / "source.json", {})
            branch = ref or meta.get("branch", "cached snapshot")
        else:
            url = "https://github.com/" + repo + ".git"
            if ref:
                branch = ref
            else:
                remote = git(None, "ls-remote", "--symref", url, "HEAD").decode()
                match = re.search(r"^ref: refs/heads/(.+)\tHEAD$", remote, re.M)
                if not match:
                    raise BuildError("Repository has no default-branch commit, or is inaccessible.")
                branch = match[1]
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                git(None, "init", "--bare", str(path))
                git(path, "remote", "add", "origin", url)
            actual = git(path, "remote", "get-url", "origin").decode().strip()
            if actual != url:
                raise BuildError("Cache origin does not match the requested repository.")
            note(f"Fetching {repo} ({branch}); full ancestry, without file blobs…")
            git(path, "fetch", "--no-tags", "--filter=blob:none", "origin", branch)
            head = git(path, "rev-parse", "FETCH_HEAD^{commit}").decode().strip()
            write_json(cache / "source.json", {"branch": branch, "head": head})
    if git(path, "rev-parse", "--is-shallow-repository").strip() == b"true":
        raise BuildError("Shallow history cannot produce a complete timeline. Supply a full clone or omit --local-repo.")
    return path, head, branch


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def collect_git(path, head):
    raw = git(path, "log", "--reverse", "--topo-order", "-z", "--encoding=UTF-8",
              "--format=%H%x00%ct%x00%an%x00%ae%x00%P%x00%s", head, "--")
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if not fields or len(fields) % 6:
        raise BuildError("Empty or malformed Git history.")
    commits = []
    for offset in range(0, len(fields), 6):
        h, timestamp, name, email, parents, subject = [s.decode("utf-8", "replace") for s in fields[offset:offset + 6]]
        commits.append({"h": h, "t": int(timestamp), "n": name, "_identity": (name, email),
                        "s": subject, "m": len(parents.split()) > 1, "f": []})
    # Stable tie order preserves the original parent-first traversal where timestamps tie.
    commits.sort(key=lambda c: c["t"])
    expected = int(git(path, "rev-list", "--count", head))
    if len(commits) != expected:
        raise BuildError("Commit count differs from git rev-list.")
    by_hash = {c["h"]: c for c in commits}
    hashes = [c["h"] for c in commits if not c["m"]]
    raw_diff = git(path, "diff-tree", "--stdin", "--root", "-r", "--name-status",
                   "--no-renames", "--no-ext-diff", "-z", input=("\n".join(hashes) + "\n").encode())
    tokens = raw_diff.split(b"\0")
    paths, path_ids, current, pos = [], {}, None, 0
    while pos < len(tokens):
        token = tokens[pos]
        pos += 1
        if not token:
            continue
        value = token.decode("utf-8", "replace")
        if value in by_hash:
            current = by_hash[value]
            continue
        if current is None or value not in ("A", "M", "D", "T") or pos >= len(tokens):
            raise BuildError("Unexpected NUL-delimited diff-tree output.")
        name = tokens[pos].decode("utf-8", "replace")
        pos += 1
        if name not in path_ids:
            path_ids[name] = len(paths)
            paths.append(name)
        current["f"].append([path_ids[name], "M" if value == "T" else value])
    head_paths = git(path, "ls-tree", "-rz", "--name-only", head).split(b"\0")
    return commits, paths, sum(bool(p) for p in head_paths)


def auth_token():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    if shutil.which("gh"):
        try:
            result = subprocess.run(["gh", "auth", "token", "--hostname", "github.com"],
                                    capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None


def fetch_authors(repo, head, cache, offline=False, refresh=False):
    path = cache / "authors" / (head + ".json")
    saved = read_json(path, {"complete": False, "commits": {}})
    mapping = saved["commits"]
    if offline or (saved["complete"] and not refresh):
        return mapping
    if refresh:
        mapping = {}
    token = auth_token()
    headers = {"User-Agent": "commit-rings-skill", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = "Bearer " + token
    page = 1
    complete = False
    try:
        while True:
            query = urllib.parse.urlencode({"sha": head, "per_page": 100, "page": page})
            req = urllib.request.Request(f"https://api.github.com/repos/{repo}/commits?{query}", headers=headers)
            with urllib.request.urlopen(req, timeout=30) as response:
                rows = json.load(response)
            if not isinstance(rows, list):
                raise BuildError("Unexpected GitHub commits response.")
            for row in rows:
                a = row.get("author")
                mapping[row["sha"]] = {"id": a["id"], "login": a["login"]} if a else None
            complete = len(rows) < 100
            write_json(path, {"complete": complete, "commits": mapping})
            if complete:
                break
            page += 1
            if page % 10 == 0:
                note(f"Resolved GitHub author pages: {page - 1}")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as exc:
        reason = f"HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
        note(f"GitHub author lookup incomplete ({reason}); keeping Git identities for unmatched commits.")
    return mapping


def group_authors(commits, mapping):
    authors, ids, identities = [], {}, set()
    matched = 0
    for i, commit in enumerate(commits):
        identity = commit.pop("_identity")
        identities.add(identity)
        account = mapping.get(commit["h"])
        key = ("github", account["id"]) if account else ("git", identity)
        matched += bool(account)
        if key not in ids:
            ids[key] = len(authors)
            authors.append({"n": account["login"] if account else commit["n"],
                            "login": account["login"] if account else None,
                            "github_id": account["id"] if account else None,
                            "aliases": [], "total": 0, "first": i, "avatar": None})
        commit["a"] = ids[key]
        author = authors[commit["a"]]
        author["total"] += 1
        if commit["n"] not in author["aliases"]:
            author["aliases"].append(commit["n"])
    return authors, len(identities), matched


def embed_avatars(authors, cache, offline=False):
    def download(author):
        account_id = author["github_id"]
        if not account_id:
            return None
        file = cache / "avatars" / (str(account_id) + ".json")
        saved = read_json(file, None)
        if saved or offline:
            return saved
        try:
            req = urllib.request.Request(f"https://avatars.githubusercontent.com/u/{int(account_id)}?s=64&v=4",
                                         headers={"User-Agent": "commit-rings-skill"})
            with urllib.request.urlopen(req, timeout=15) as response:
                content, mime = response.read(200_001), response.headers.get_content_type()
            if mime in ("image/png", "image/jpeg", "image/gif", "image/webp") and len(content) <= 200_000:
                result = "data:" + mime + ";base64," + base64.b64encode(content).decode()
                write_json(file, result)
                return result
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        for author, avatar in zip(authors, pool.map(download, authors)):
            author["avatar"] = avatar


def path_category(path):
    p = path.lower()
    parts = p.split("/")
    name = parts[-1]
    suffix = Path(name).suffix
    if any(s in ("tests", "test", "__tests__", "spec", "specs") for s in parts[:-1]) or name.startswith("test_") or re.search(r"[._-](test|spec)\.", name):
        return 2
    if any(s in ("docs", "doc", "documentation") for s in parts[:-1]) or suffix in (".md", ".mdx", ".rst", ".adoc") or name.startswith(("readme", "license", "changelog")):
        return 1
    if any(s in (".github", ".gitlab", ".devcontainer", ".vscode") for s in parts[:-1]) or suffix in (".toml", ".yaml", ".yml", ".ini", ".cfg", ".lock") or name.startswith(("dockerfile", "makefile", "requirements", ".git")) or name in ("package.json", "package-lock.json", "setup.py", "setup.cfg", "cmakelists.txt", "cargo.lock"):
        return 3
    if any(s in ("assets", "images", "data", "datasets", "static", "media") for s in parts[:-1]) or suffix in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".csv", ".json", ".jsonl", ".parquet", ".pdf", ".mp4", ".woff", ".ttf"):
        return 4
    return 0


def classify(commits, paths):
    for commit in commits:
        counts = Counter(path_category(paths[p]) for p, _ in commit["f"])
        commit["g"] = min(counts, key=lambda k: (-counts[k], k)) if counts and not commit["m"] else 5


def render(data, template=None):
    source = (template or SKILL_ROOT / "assets" / "history.template.html").read_text(encoding="utf-8")
    owner, repo_name = data["repo"].split("/")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    replacements = {"REPO_NAME": html.escape(repo_name, quote=True), "OWNER": html.escape(owner, quote=True),
                    "REPO_FULL": html.escape(data["repo"], quote=True), "REPO_CAPTION": html.escape(repo_name.upper()),
                    "HISTORY_JSON": payload}
    for key, value in replacements.items():
        source = source.replace("@@" + key + "@@", value)
    return source


def build(args):
    repo = normalize_repo(args.repo)
    cache = Path(args.cache_dir).expanduser().resolve() / repo.replace("/", "--")
    cache.mkdir(parents=True, exist_ok=True)
    path, head, branch = source_repo(repo, cache, args.local_repo, args.ref, args.offline)
    note(f"Reading Git history at {head[:12]}…")
    commits, paths, head_files = collect_git(path, head)
    note(f"{len(commits):,} commits, {len(paths):,} historical file paths")
    if len(commits) > 20_000:
        note("Large history: all commits will be retained; browser interaction may be slow.")
    mapping = {} if args.no_github else fetch_authors(repo, head, cache, args.offline, args.refresh_authors)
    authors, identity_count, matched = group_authors(commits, mapping)
    embed_avatars(authors, cache, args.offline or args.no_github)
    classify(commits, paths)
    data = {"schemaVersion": 1, "repo": repo, "head": head, "branch": branch, "shallow": False,
            "generated": datetime.now(timezone.utc).isoformat(), "commits": commits, "paths": paths,
            "authors": authors, "headFiles": head_files, "gitAuthorIdentities": identity_count,
            "authorGrouping": "GitHub author account ID; unresolved (Git name, email) pairs stay separate",
            "categories": CATEGORIES, "timezoneOffsetMinutes": 480,
            "identityCoverage": {"queried": sum(c["h"] in mapping for c in commits), "matched": matched,
                                 "total": len(commits)},
            "source": "local" if args.local_repo else "github"}
    if sum(a["total"] for a in authors) != len(commits):
        raise BuildError("Author totals do not match commit count.")
    output = Path(args.output or (repo.split("/")[1] + "-history.html")).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    content = render(data)
    # Atomic replacement keeps an earlier good artifact intact if writing fails.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(output)
    summary = {"output": str(output), "repo": repo, "head": head, "branch": branch,
               "commits": len(commits), "contributors": len(authors), "historicalPaths": len(paths),
               "githubMatchedCommits": matched, "githubQueriedCommits": data["identityCoverage"]["queried"],
               "embeddedAvatars": sum(bool(a["avatar"]) for a in authors), "bytes": output.stat().st_size}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", help="owner/repo or a GitHub repository URL")
    parser.add_argument("--output", "-o", help="Default: ./<repo>-history.html")
    parser.add_argument("--local-repo", help="Read an existing full clone without fetching or changing it")
    parser.add_argument("--ref", help="Git branch, tag or commit; default: remote default branch / local HEAD")
    parser.add_argument("--cache-dir", default=str(Path.home() / ".cache" / "commit-rings-skill"))
    parser.add_argument("--offline", action="store_true", help="Use only local Git objects and cached identity/avatar data")
    parser.add_argument("--no-github", action="store_true", help="Skip GitHub identity lookup; display Git signatures")
    parser.add_argument("--refresh-authors", action="store_true", help="Refresh GitHub identity mapping for this snapshot")
    args = parser.parse_args()
    try:
        build(args)
    except (BuildError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        note("Error: " + str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
