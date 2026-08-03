#!/usr/bin/env python3
"""
mdreview - review changed Markdown as rendered prose, and capture line comments.

Renders the new version of every changed .md in a single column with changes
highlighted in place, shows real source line numbers in the gutter, and lets you
attach comments to lines. Exports the comments as a GitHub review payload, a
ready-to-run gh command, or a prompt for Claude.

It works out which lines GitHub will actually accept an inline comment on (a
line has to sit inside a diff hunk, or the reviews API returns 422) and routes
anything outside that into the review summary instead, so nothing is lost.

Usage
    mdreview                          working tree vs HEAD
    mdreview main                     everything on this branch vs main
    mdreview main...HEAD              explicit range
    mdreview --commit 58c5e9a         one commit
    mdreview main --pr 214            name the PR the export should target
    mdreview main --summary "Doc pass on MM-8141"
    mdreview main --no-breaks         soft-wrap newlines, like github.com renders
    mdreview main -o review.html --no-open

Keys
    j / k   next / previous change      c   comment on the current block
    [ / ]   previous / next file        Esc close the editor

Requires: python-markdown, lxml.  Optional: gh, for PR auto-detection.
    pip install markdown lxml
"""
import argparse
import csv as csvlib
import difflib
import html as htmllib
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    import markdown
except ImportError:
    import subprocess as _sp
    print("Installing dependencies...", flush=True)
    _sp.check_call([sys.executable, "-m", "pip", "install", "markdown", "lxml"])
    import markdown

try:
    from lxml.html.diff import htmldiff
    HAVE_HTMLDIFF = True
except ImportError:
    HAVE_HTMLDIFF = False

WORKTREE = object()  # sentinel for "read from disk"

MD_EXTENSIONS = ["tables", "fenced_code", "sane_lists", "attr_list", "nl2br"]


# ---------------------------------------------------------------- local server

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _remote_branches():
    out = git("ls-remote", "--heads", "origin", check=False)
    branches = []
    for line in out.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            ref = parts[1].strip()
            if ref.startswith("refs/heads/"):
                branches.append("origin/" + ref[len("refs/heads/"):])
    return sorted(branches)


def _make_handler(render_fn):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path, _, qs = self.path.partition("?")
            if path == "/branches":
                self._json(200, _remote_branches())
                return
            if path == "/":
                ref = urllib.parse.parse_qs(qs).get("ref", [None])[0]
                html, err = render_fn(ref)
                if err:
                    html = f"<html><body><p>{htmllib.escape(err)}</p></body></html>"
                data = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            if self.path != "/post":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            repo, pr = body.get("repo"), body.get("pr")
            if not repo or not pr:
                self._reply(400, "Missing repo or PR number — open the PR before posting.")
                return
            payload = {
                "body": body.get("summary", ""),
                "event": "COMMENT",
                "comments": body.get("comments", []),
            }
            if body.get("commit"):
                payload["commit_id"] = body["commit"]
            r = subprocess.run(
                ["gh", "api", f"repos/{repo}/pulls/{pr}/reviews",
                 "--method", "POST", "--input", "-"],
                input=json.dumps(payload), capture_output=True,
                text=True, encoding="utf-8", stdin=subprocess.DEVNULL,
            )
            if r.returncode == 0:
                self._reply(200, "Posted.")
            else:
                self._reply(500, r.stderr.strip() or "gh api call failed.")

        def _reply(self, code, msg):
            self._json(code, {"message": msg})

        def _json(self, code, obj):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    return Handler


def serve(render_fn, no_open=False):
    port = _free_port()
    httpd = HTTPServer(("127.0.0.1", port), _make_handler(render_fn))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    print(f"Serving at {url}  (Ctrl+C to stop)", flush=True)
    if not no_open:
        webbrowser.open(url)
    try:
        thread.join()
    except KeyboardInterrupt:
        httpd.shutdown()


# ---------------------------------------------------------------- git plumbing

_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

def git(*args, check=True):
    r = subprocess.run(
        ["git", *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, env=_GIT_ENV,
    )
    if check and r.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed:\n{r.stderr.strip()}")
    return r.stdout


def repo_root():
    out = git("rev-parse", "--show-toplevel").strip()
    if not out:
        sys.exit("Not inside a git repository.")
    return out


def resolve_refs(spec, commit):
    """Return (base_ref, head_ref) where head_ref may be the WORKTREE sentinel."""
    if commit:
        return f"{commit}^", commit
    if not spec:
        return "HEAD", WORKTREE
    if "..." in spec:
        a, _, b = spec.partition("...")
        merge_base = git("merge-base", a or "HEAD", b or "HEAD").strip()
        return merge_base, (b or WORKTREE) if b else WORKTREE
    if ".." in spec:
        a, _, b = spec.partition("..")
        return a or "HEAD", (b or WORKTREE) if b else WORKTREE
    # bare ref: treat as base, compare against working tree
    return spec, WORKTREE


def changed_markdown(base, head):
    """Return list of (status, path). Status is one of A M D R."""
    args = ["diff", "--name-status", "--find-renames", base]
    if head is not WORKTREE:
        args.append(head)
    args += ["--", "*.md", "*.markdown", "*.mdx"]
    out = git(*args)
    files = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][0]
        path = parts[-1]
        files.append((status, path))
    return sorted(files, key=lambda f: f[1])


def numstat(base, head):
    """Real git +/- per path, so the tallies match git and GitHub Desktop."""
    args = ["diff", "--numstat", "--find-renames", base]
    if head is not WORKTREE:
        args.append(head)
    args += ["--", "*.md", "*.markdown", "*.mdx"]
    stats = {}
    for line in git(*args).splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            a = 0 if parts[0] == "-" else int(parts[0])
            d = 0 if parts[1] == "-" else int(parts[1])
            stats[parts[-1]] = (a, d)
    return stats


HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def commentable_lines(base, head, path):
    """RIGHT-side line numbers GitHub will accept a review comment on.

    A line is commentable when it appears inside a diff hunk, which includes
    the context lines GitHub shows either side of a change, not just the
    additions. Anything outside a hunk gets rejected with a 422, so we work
    the set out up front rather than discovering it at post time.
    """
    args = ["diff", "-U3", "--find-renames", base]
    if head is not WORKTREE:
        args.append(head)
    args += ["--", path]
    lines, cur = set(), None
    for line in git(*args, check=False).splitlines():
        m = HUNK.match(line)
        if m:
            cur = int(m.group(1))
            continue
        if cur is None or line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("+") or line.startswith(" ") or line == "":
            lines.add(cur)
            cur += 1
        elif line.startswith("-"):
            continue
        else:
            cur = None
    return lines


_MD_EXTS = (".md", ".markdown", ".mdx", ".csv")

def all_markdown(ref=None):
    if ref is None:
        out = git("ls-files", "--", "*.md", "*.markdown", "*.mdx", "*.csv")
    else:
        out = git("ls-tree", "-r", "--name-only", ref)
    return sorted(p for p in out.splitlines() if p.endswith(_MD_EXTS))


def render_csv(text):
    rows = list(csvlib.reader(io.StringIO(text or "")))
    if not rows:
        return "<p><em>Empty file.</em></p>"
    esc = htmllib.escape
    head = "".join(f"<th>{esc(c)}</th>" for c in rows[0])
    body = "".join(
        "<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>"
        for row in rows[1:] if any(c.strip() for c in row)
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def build_csv_blocks(text):
    lines = (text or "").count("\n") + 1
    return [{
        "state": "same", "kind": "table",
        "html": render_csv(text), "src": text,
        "line": 1, "end": lines,
        "side": "RIGHT", "anchor": 1, "can": True,
    }]


def build_view_blocks(text):
    pairs = split_blocks(text or "")
    blocks = []
    for start, block in pairs:
        end = start + len(block.splitlines()) - 1
        blocks.append({
            "state": "same", "kind": block_kind(block),
            "html": render(block), "src": block,
            "line": start, "end": end,
            "side": "RIGHT", "anchor": start, "can": True,
        })
    return blocks


def read_blob(ref, path):
    if ref is WORKTREE:
        full = os.path.join(repo_root(), path)
        if not os.path.exists(full):
            return None
        with open(full, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    r = subprocess.run(
        ["git", "show", f"{ref}:{path}"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, env=_GIT_ENV,
    )
    return r.stdout if r.returncode == 0 else None


# ------------------------------------------------------------ block splitting

FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def split_blocks(text):
    """Split markdown into (start_line, text) blocks, keeping fences intact.

    start_line is 1-based and refers to the file the text came from, which is
    what GitHub's review API wants.
    """
    if not text:
        return []
    blocks, buf, fence, start = [], [], None, 1
    for lineno, line in enumerate(text.splitlines(), 1):
        m = FENCE.match(line)
        if fence is None and m:
            fence = m.group(1)[0] * 3
            if not buf:
                start = lineno
            buf.append(line)
            continue
        if fence is not None:
            buf.append(line)
            if FENCE.match(line) and line.strip().startswith(fence):
                blocks.append((start, "\n".join(buf)))
                buf, fence = [], None
            continue
        if line.strip() == "":
            if buf:
                blocks.append((start, "\n".join(buf)))
                buf = []
        else:
            if not buf:
                start = lineno
            buf.append(line)
    if buf:
        blocks.append((start, "\n".join(buf)))
    return blocks


def block_kind(block):
    s = block.lstrip()
    if s.startswith("#"):
        return "heading"
    if s.startswith("|"):
        return "table"
    if FENCE.match(s):
        return "code"
    return "prose"


# --------------------------------------------------------------- diff + render

def render(md_text):
    return markdown.markdown(md_text, extensions=MD_EXTENSIONS)


def inline_marked(old_block, new_block):
    """Word-level ins/del inside a changed block. Falls back to whole-block mark."""
    new_html = render(new_block)
    if not HAVE_HTMLDIFF:
        return new_html, False
    try:
        merged = htmldiff(render(old_block), new_html)
        # htmldiff sometimes mangles tables; only trust it if it kept the tags
        if block_kind(new_block) == "table" and "<table" not in merged:
            return new_html, False
        if not merged.strip():
            return new_html, False
        return merged, True
    except Exception:
        return new_html, False


def build_file_view(old_text, new_text, ok_lines=frozenset()):
    """Return list of rendered block dicts carrying source line anchors."""
    old_pairs = split_blocks(old_text)
    new_pairs = split_blocks(new_text)
    old_blocks = [b for _, b in old_pairs]
    new_blocks = [b for _, b in new_pairs]
    new_starts = [n for n, _ in new_pairs]
    old_starts = [n for n, _ in old_pairs]

    def emit(idx, text, state, from_old=False, inline=None):
        start = (old_starts if from_old else new_starts)[idx]
        span = len(text.splitlines())
        end = start + span - 1
        # anchor on the first line in this block GitHub will accept
        anchor = next((n for n in range(start, end + 1) if n in ok_lines), None)
        d = {"state": state, "kind": block_kind(text),
             "line": start, "end": end, "side": "LEFT" if from_old else "RIGHT",
             "anchor": anchor, "can": bool(anchor) and not from_old}
        if inline is not None:
            d["inline"] = inline
        return d

    sm = difflib.SequenceMatcher(None, old_blocks, new_blocks, autojunk=False)
    out, added, removed = [], 0, 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(j1, j2):
                d = emit(k, new_blocks[k], "same")
                d["html"] = render(new_blocks[k])
                d["src"] = new_blocks[k]
                out.append(d)
        elif tag == "insert":
            for k in range(j1, j2):
                d = emit(k, new_blocks[k], "added")
                d["html"] = render(new_blocks[k])
                d["src"] = new_blocks[k]
                out.append(d)
        elif tag == "delete":
            for k in range(i1, i2):
                d = emit(k, old_blocks[k], "removed", from_old=True)
                d["html"] = render(old_blocks[k])
                out.append(d)
        else:  # replace
            olds, news = old_blocks[i1:i2], new_blocks[j1:j2]
            pairs = min(len(olds), len(news))
            for k in range(pairs):
                h, marked = inline_marked(olds[k], news[k])
                d = emit(j1 + k, news[k], "changed", inline=marked)
                d["html"] = h
                d["src"] = news[k]
                out.append(d)
            for k in range(pairs, len(news)):
                d = emit(j1 + k, news[k], "added")
                d["html"] = render(news[k])
                d["src"] = news[k]
                out.append(d)
            for k in range(pairs, len(olds)):
                d = emit(i1 + k, olds[k], "removed", from_old=True)
                d["html"] = render(olds[k])
                out.append(d)
    return out, added, removed


# ------------------------------------------------------------------ page build

CSS = r"""
:root{
  --bg:#0d1117; --panel:#161b22; --panel-2:#1c2330; --line:#2d3748;
  --ink:#e2e8f0; --ink-dim:#94a3b8; --ink-faint:#64748b;
  --add:#3fb950; --add-bg:#0d2113; --add-edge:#238636;
  --del:#f85149; --del-bg:#2a1218; --del-edge:#7d2222;
  --edit:#d29922; --accent:#58a6ff; --warn:#e3b341;
  --prose: ui-sans-serif,-apple-system,"Segoe UI",Inter,Roboto,sans-serif;
  --mono: ui-monospace,"Cascadia Code","JetBrains Mono",Menlo,Consolas,monospace;
  --r:6px;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--prose);
  font-size:15px;line-height:1.7;-webkit-font-smoothing:antialiased}
.shell{display:grid;grid-template-columns:300px 1fr;min-height:100vh}
button{font:inherit}

/* ---- sidebar ---- */
aside{background:var(--panel);border-right:1px solid var(--line);
  position:sticky;top:0;height:100vh;overflow-y:auto;overflow-x:hidden}
.meta{padding:20px 18px 16px;border-bottom:1px solid var(--line)}
.meta h1{font-size:13px;font-weight:600;margin:0 0 4px;line-height:1.4;
  color:var(--ink);letter-spacing:-.01em}
.meta .range{font-family:var(--mono);font-size:10.5px;color:var(--ink-faint);
  word-break:break-all;line-height:1.5}
.tally{margin-top:8px;font-family:var(--mono);font-size:11.5px;display:flex;gap:8px}
.branch-sel-wrap{padding:10px 14px;border-bottom:1px solid var(--line)}
.branch-sel{width:100%;background:var(--panel-2);border:1px solid var(--line);
  border-radius:5px;color:var(--ink);font-size:11.5px;padding:5px 8px;
  cursor:pointer;appearance:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%2364748b'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 8px center;padding-right:24px}
.branch-sel:focus{outline:2px solid var(--accent);outline-offset:1px}
.tally .p{color:var(--add)} .tally .m{color:var(--del)}
.target-note{margin-top:10px;font-size:11px;line-height:1.55;color:var(--ink-faint)}
.target-note b{color:var(--ink-dim);font-weight:600}
.target-note.bad{color:var(--warn)}
.files-hd{padding:14px 18px 5px;font-size:10.5px;letter-spacing:.09em;
  text-transform:uppercase;color:var(--ink-faint);font-weight:500}
.file-btn{display:block;width:100%;text-align:left;background:none;border:0;
  color:var(--ink-dim);padding:8px 36px 8px 16px;cursor:pointer;
  border-left:2px solid transparent;position:relative;overflow:hidden}
.file-btn:hover{background:var(--panel-2);color:var(--ink)}
.file-btn.on{background:var(--panel-2);color:var(--ink);border-left-color:var(--accent)}
.file-btn .dir{display:block;font-size:10.5px;color:var(--ink-faint);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:1px}
.file-btn .fname{display:block;font-size:12.5px;font-weight:500;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.file-btn .counts{font-family:var(--mono);font-size:10.5px;margin-top:3px;
  color:var(--ink-faint)}
.file-btn.on .counts .p{color:var(--add)}
.file-btn.on .counts .m{color:var(--del)}
.file-btn .pin{position:absolute;right:12px;top:50%;transform:translateY(-50%);
  background:var(--accent);color:#0d1117;font-family:var(--mono);font-size:10px;
  font-weight:700;border-radius:9px;padding:2px 6px}
.file-btn .pin:empty{display:none}

/* ---- top bar ---- */
main{min-width:0}
.bar{position:sticky;top:0;z-index:5;background:rgba(13,17,23,.92);
  backdrop-filter:blur(10px);border-bottom:1px solid var(--line);
  padding:10px 36px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.bar .path{font-family:var(--mono);font-size:12px;color:var(--ink-dim);
  margin-right:auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar .path b{color:var(--ink);font-weight:600}
.tog{display:inline-flex;align-items:center;gap:6px;font-size:12px;
  color:var(--ink-dim);cursor:pointer;user-select:none}
.tog input{accent-color:var(--accent);margin:0}
.nav{display:flex;align-items:center;gap:6px}
.nav button{background:var(--panel-2);border:1px solid var(--line);
  color:var(--ink-dim);border-radius:var(--r);padding:3px 10px;font-size:11.5px;
  cursor:pointer;transition:color .1s,border-color .1s}
.nav button:hover{color:var(--ink);border-color:var(--ink-faint)}
.nav .pos{font-family:var(--mono);font-size:11px;color:var(--ink-faint);
  min-width:52px;text-align:center}

.doc{padding:32px 40px 45vh}

/* ---- blocks: line gutter + change ribbon ---- */
.blk{position:relative;padding:3px 12px 3px 76px;margin:0 0 3px;
  border-radius:0 var(--r) var(--r) 0}
.blk::before{content:"";position:absolute;left:62px;top:4px;bottom:4px;width:3px;
  border-radius:2px;background:transparent}
.blk.added::before{background:var(--add-edge)}
.blk.changed::before{background:var(--edit)}
.blk.removed::before{background:var(--del-edge)}
.blk.added{background:var(--add-bg)}
.blk.changed{background:rgba(210,153,34,.07)}
.blk.removed{background:var(--del-bg);opacity:.72}
.blk.target{outline:2px solid var(--accent);outline-offset:2px}
.blk.has-note{box-shadow:inset 3px 0 0 var(--accent)}

.gut{position:absolute;left:0;top:2px;width:54px;display:flex;
  align-items:flex-start;justify-content:flex-end;gap:3px;user-select:none}
.gut .ln{font-family:var(--mono);font-size:11px;color:var(--ink-faint);
  line-height:2;letter-spacing:-.02em;opacity:.6}
.gut .plus{opacity:0;background:var(--accent);color:#0d1117;border:0;
  border-radius:4px;width:16px;height:16px;line-height:1;font-size:12px;
  font-weight:700;cursor:pointer;margin-top:5px;padding:0;flex-shrink:0}
.blk:hover .gut .ln{opacity:1}
.blk:hover .gut .plus{opacity:1}
.gut .plus:focus-visible{opacity:1;outline:2px solid var(--ink)}
.blk.nocomment .gut .plus{background:var(--ink-faint);color:var(--panel)}
.blk.removed .gut .plus{display:none}

/* word-level marks */
ins{background:rgba(63,185,80,.22);color:#aff4c6;text-decoration:none;
  border-radius:2px;padding:0 1px}
del{background:rgba(248,81,73,.18);color:#f1a9a5;border-radius:2px;padding:0 1px}
body.hide-del del{display:none}
body.hide-del .del-only{display:none}
body.hide-removed .blk.removed{display:none}
body.only-changes .blk.same{display:none}
.rm-tag{position:absolute;right:8px;top:4px;font-family:var(--mono);font-size:10px;
  letter-spacing:.06em;color:var(--del);opacity:.7}

/* ---- comment editor + cards ---- */
.editor{margin:10px 0 4px;background:var(--panel);border:1px solid var(--line);
  border-radius:8px;padding:14px}
.editor .hd{display:flex;align-items:center;gap:10px;margin-bottom:10px;
  font-size:12px;color:var(--ink-dim)}
.editor .hd .anchor{font-family:var(--mono)}
.editor .hd input[type=number]{width:70px;background:var(--panel-2);
  border:1px solid var(--line);color:var(--ink);border-radius:5px;
  padding:2px 6px;font-family:var(--mono);font-size:12px}
.editor textarea{width:100%;background:var(--panel-2);border:1px solid var(--line);
  color:var(--ink);border-radius:var(--r);padding:9px 12px;font-family:var(--prose);
  font-size:14px;line-height:1.6;resize:vertical;min-height:80px;
  transition:border-color .15s}
.editor textarea:focus{outline:none;border-color:var(--accent)}
.editor textarea.sugg{font-family:var(--mono);font-size:12.5px;min-height:56px;
  margin-top:8px}
.editor .row{display:flex;align-items:center;gap:10px;margin-top:10px;flex-wrap:wrap}
.editor .row .sp{margin-right:auto}
.btn{background:var(--panel-2);border:1px solid var(--line);color:var(--ink-dim);
  border-radius:var(--r);padding:5px 13px;font-size:12.5px;cursor:pointer;
  transition:color .1s,border-color .1s}
.btn:hover{color:var(--ink);border-color:var(--ink-dim)}
.btn.pri{background:#1f6feb;border-color:#1f6feb;color:#fff}
.btn.pri:hover{background:#3b82f6;border-color:#3b82f6;color:#fff}
.btn.danger:hover{color:var(--del);border-color:var(--del-edge)}
.flag{font-size:11.5px;color:var(--warn);line-height:1.45}

.note{margin:10px 0 4px;background:var(--panel);border:1px solid var(--line);
  border-left:3px solid var(--accent);border-radius:0 8px 8px 0;padding:11px 14px}
.note .nh{display:flex;gap:9px;align-items:center;font-family:var(--mono);
  font-size:11px;color:var(--ink-faint);margin-bottom:6px}
.note .nh .warn{color:var(--warn)}
.note .nb{font-size:13.5px;white-space:pre-wrap;line-height:1.6;color:var(--ink-dim)}
.note pre.sg{background:var(--panel-2);border:1px solid var(--line);
  border-radius:var(--r);padding:8px 10px;margin:8px 0 0;font-family:var(--mono);
  font-size:12px;overflow:auto;white-space:pre-wrap}
.note .na{margin-top:8px;display:flex;gap:7px}
.note .na button{background:none;border:0;color:var(--ink-faint);font-size:11.5px;
  cursor:pointer;padding:0;transition:color .1s}
.note .na button:hover{color:var(--accent)}

/* ---- review tray ---- */
.tray{position:fixed;right:24px;bottom:24px;z-index:20;width:420px;
  max-width:calc(100vw - 48px);background:var(--panel);border:1px solid var(--line);
  border-radius:10px;box-shadow:0 16px 48px rgba(0,0,0,.6);overflow:hidden}
.tray.min{width:auto}
.tray .th{display:flex;align-items:center;gap:10px;padding:12px 16px;cursor:pointer;
  font-size:13px;font-weight:600;letter-spacing:-.01em}
.tray .th .n{background:var(--accent);color:#0d1117;font-family:var(--mono);
  font-size:10.5px;font-weight:700;border-radius:9px;padding:2px 7px}
.tray .th .chev{margin-left:auto;color:var(--ink-faint);font-size:10px}
.tray .tb{border-top:1px solid var(--line);max-height:min(44vh,380px);overflow:auto;
  padding:6px 0}
.tray.min .tb,.tray.min .tf{display:none}
.tray .grp{padding:8px 16px 4px;font-family:var(--mono);font-size:10px;
  color:var(--ink-faint);text-transform:uppercase;letter-spacing:.08em}
.tray .itm{display:flex;gap:10px;padding:7px 16px;font-size:12.5px;cursor:pointer;
  border-left:2px solid transparent;align-items:baseline}
.tray .itm:hover{background:var(--panel-2);border-left-color:var(--accent)}
.tray .itm .l{font-family:var(--mono);font-size:10.5px;color:var(--ink-faint);
  flex:0 0 38px}
.tray .itm .l.warn{color:var(--warn)}
.tray .itm .t{color:var(--ink-dim);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;flex:1;min-width:0}
.tray .tf{border-top:1px solid var(--line);padding:12px 16px;display:flex;
  gap:8px;flex-wrap:wrap;align-items:center}
.tray .tf .hint{font-size:11px;color:var(--ink-faint);width:100%;line-height:1.55;
  margin-top:4px}
.tray .empty{padding:18px 16px;font-size:12.5px;color:var(--ink-faint);line-height:1.6}
.toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);
  background:var(--panel-2);border:1px solid var(--line);color:var(--ink);
  padding:9px 18px;border-radius:8px;font-size:13px;z-index:40;opacity:0;
  pointer-events:none}
.toast.on{opacity:1}

/* ---- prose ---- */
.doc h1,.doc h2,.doc h3,.doc h4{line-height:1.3;margin:1.4em 0 .55em;
  font-weight:650;letter-spacing:-.02em;text-wrap:balance}
.doc h1{font-size:27px;padding-bottom:.35em;border-bottom:1px solid var(--line)}
.doc h2{font-size:20px;padding-bottom:.3em;border-bottom:1px solid var(--line)}
.doc h3{font-size:16.5px} .doc h4{font-size:14.5px;color:var(--ink-dim)}
.blk.added h1,.blk.added h2,.blk.changed h1,.blk.changed h2{border-bottom-color:transparent}
.doc p{margin:.7em 0}
.doc ul,.doc ol{margin:.65em 0;padding-left:1.6em}
.doc li{margin:.3em 0}
.doc a{color:var(--accent);text-decoration:none}
.doc a:hover{text-decoration:underline}
.doc code{font-family:var(--mono);font-size:.855em;background:#1c2435;
  border:1px solid var(--line);border-radius:4px;padding:.1em .35em}
.doc pre{background:#111827;border:1px solid var(--line);border-radius:var(--r);
  padding:14px 16px;overflow:auto}
.doc pre code{background:none;border:0;padding:0;font-size:12.5px}
.doc blockquote{margin:.8em 0;padding:.1em 0 .1em 16px;
  border-left:3px solid var(--line);color:var(--ink-dim)}
.doc table{border-collapse:collapse;margin:.9em 0;font-size:13.5px;display:block;
  overflow-x:auto;max-width:100%}
.doc th,.doc td{border:1px solid var(--line);padding:7px 12px;text-align:left;
  vertical-align:top}
.doc th{background:var(--panel-2);font-weight:600}
.doc hr{border:0;border-top:1px solid var(--line);margin:1.8em 0}
.doc table{font-variant-numeric:tabular-nums}
.doc tbody tr:nth-child(even){background:rgba(255,255,255,.03)}
.doc tbody tr:hover{background:rgba(88,166,255,.06)}

/* ---- view mode: continuous document reader ---- */
body.view-mode .blk.same{background:transparent;margin-bottom:0;
  border-radius:0;padding-top:1px;padding-bottom:1px}
body.view-mode .blk.same::before{display:none}
body.view-mode .blk.same:hover{background:rgba(255,255,255,.025);
  border-radius:0 var(--r) var(--r) 0}
body.view-mode .blk.same .gut .ln{opacity:.35}
body.view-mode .blk.same:hover .gut .ln{opacity:.75}
body.view-mode .tally{display:none}
body.view-mode .tog{display:none}
body.view-mode .nav .pos{display:none}

@media (max-width:900px){
  .shell{grid-template-columns:1fr}
  aside{position:static;height:auto}
  .doc{padding:20px 16px 40vh}
  .blk{padding-left:64px}
  .tray{right:10px;left:10px;bottom:10px;width:auto}
}
@media (prefers-reduced-motion:no-preference){
  .toast{transition:opacity .18s}
  .gut .plus{transition:opacity .12s}
}
"""

JS = r"""
const FILES = __FILES__;
const REVIEW = __REVIEW__;
const $ = s => document.querySelector(s);
const esc = t => t.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

let cur = 0, notes = [], seq = 1;

/* ---------- persistence. This is a local file:// page, so localStorage is
   just a scratchpad that survives a refresh or a re-run of the script. ------ */
const KEY = 'mdreview:' + REVIEW.key;
function save(){
  try{ localStorage.setItem(KEY, JSON.stringify({notes, seq})); }catch(e){}
}
function load(){
  try{
    const raw = localStorage.getItem(KEY);
    if(!raw) return;
    const d = JSON.parse(raw);
    notes = d.notes || []; seq = d.seq || 1;
  }catch(e){}
}

/* ---------- rendering ---------- */
const LEAF = 'p,h1,h2,h3,h4,h5,h6,li,blockquote,pre,td,th';
const norm = t => t.replace(/\s+/g,'');
function markDelOnly(root){
  root.querySelectorAll(LEAF).forEach(el=>{
    const dels = [...el.querySelectorAll('del')];
    if(!dels.length) return;
    const all = norm(el.textContent);
    if(all && all === norm(dels.map(d=>d.textContent).join('')))
      el.classList.add('del-only');
  });
  root.querySelectorAll('ul,ol,tr,thead,tbody,table').forEach(el=>{
    const k = [...el.children];
    if(k.length && k.every(c=>c.classList.contains('del-only')))
      el.classList.add('del-only');
  });
}

function lineLabel(b){
  return b.end > b.line ? b.line + '\u2013' + b.end : String(b.line);
}

function paint(i){
  cur = i;
  const f = FILES[i];
  document.querySelectorAll('.file-btn').forEach((b,k)=>b.classList.toggle('on',k===i));
  const parts = f.path.split(/[\/\\]/), name = parts.pop();
  $('.bar .path').innerHTML = '<span class="dir">' +
    (parts.length ? esc(parts.join('/')) + '/' : '') + '</span><b>' + esc(name) + '</b>';

  const inner = $('.doc-inner');
  inner.innerHTML = f.blocks.map((b,bi)=>{
    const cls = ['blk', b.state];
    if(!b.can && b.state !== 'removed') cls.push('nocomment');
    const plus = b.state === 'removed' ? '' :
      '<button class="plus" data-b="'+bi+'" title="'+
      (b.can ? 'Comment on line '+b.anchor
             : 'Outside the diff. Comment goes in the review summary instead.')+
      '">+</button>';
    return '<div class="'+cls.join(' ')+'" id="b'+bi+'" data-line="'+b.line+'">' +
      '<div class="gut"><span class="ln">'+lineLabel(b)+'</span>'+plus+'</div>' +
      (b.state==='removed' ? '<span class="rm-tag">REMOVED</span>' : '') +
      b.html + '<div class="slot"></div></div>';
  }).join('\n');

  markDelOnly(inner);
  inner.querySelectorAll('.plus').forEach(btn=>
    btn.addEventListener('click', e=>{ e.stopPropagation(); openEditor(+btn.dataset.b); }));
  drawNotes();
  window.scrollTo({top:0});
  reindex();
}

/* ---------- comment editor ---------- */
function closeEditors(){ document.querySelectorAll('.editor').forEach(e=>e.remove()); }

function openEditor(bi, editId){
  closeEditors();
  const f = FILES[cur], b = f.blocks[bi];
  const existing = editId ? notes.find(n=>n.id===editId) : null;
  const anchor = existing ? existing.line : (b.anchor || b.line);
  const src = b.src || '';

  const ed = document.createElement('div');
  ed.className = 'editor';
  ed.innerHTML =
    '<div class="hd">Comment on <span class="anchor">' + esc(f.path.split(/[\/\\]/).pop()) +
      '</span> line <input type="number" class="ln-in" value="'+anchor+'" min="1"></div>' +
    '<textarea class="body" placeholder="What needs changing, and why?"></textarea>' +
    '<div class="row"><label class="tog"><input type="checkbox" class="sg-on"> ' +
      'Suggest a replacement</label></div>' +
    '<textarea class="sugg" style="display:none"></textarea>' +
    (b.can ? '' : '<div class="flag">Line '+b.line+' is outside the diff, so GitHub ' +
      'will not take an inline comment here. This one goes into the review summary ' +
      'with a file and line reference.</div>') +
    '<div class="row"><span class="sp"></span>' +
      (existing ? '<button class="btn danger" data-a="del">Delete</button>' : '') +
      '<button class="btn" data-a="cancel">Cancel</button>' +
      '<button class="btn pri" data-a="save">'+(existing?'Update':'Add comment')+'</button></div>';

  document.querySelector('#b'+bi+' .slot').appendChild(ed);
  const body = ed.querySelector('.body'), sgOn = ed.querySelector('.sg-on'),
        sg = ed.querySelector('.sugg'), lnIn = ed.querySelector('.ln-in');
  if(existing){
    body.value = existing.body;
    if(existing.suggestion){ sgOn.checked = true; sg.style.display='block'; sg.value = existing.suggestion; }
  }
  sgOn.addEventListener('change', ()=>{
    sg.style.display = sgOn.checked ? 'block' : 'none';
    if(sgOn.checked && !sg.value) sg.value = src;
    if(sgOn.checked) sg.focus();
  });
  ed.addEventListener('click', e=>{
    const a = e.target.dataset.a;
    if(!a) return;
    if(a==='cancel') return closeEditors();
    if(a==='del'){ notes = notes.filter(n=>n.id!==editId); save(); closeEditors(); drawNotes(); return; }
    const text = body.value.trim();
    if(!text) return body.focus();
    const rec = {
      id: existing ? existing.id : 'c'+(seq++),
      path: f.path, line: Math.max(1, +lnIn.value || anchor),
      side: 'RIGHT', body: text,
      suggestion: sgOn.checked ? sg.value.replace(/\s+$/,'') : '',
      inline: b.can, block: bi,
      quote: (b.src||'').split('\n')[0].slice(0,90)
    };
    if(existing) notes = notes.map(n=>n.id===rec.id?rec:n); else notes.push(rec);
    save(); closeEditors(); drawNotes();
  });
  body.focus();
  ed.scrollIntoView({block:'nearest', behavior:'smooth'});
}

function drawNotes(){
  document.querySelectorAll('.note').forEach(n=>n.remove());
  document.querySelectorAll('.blk').forEach(b=>b.classList.remove('has-note'));
  const f = FILES[cur];
  notes.filter(n=>n.path===f.path).forEach(n=>{
    const host = document.querySelector('#b'+n.block+' .slot');
    if(!host) return;
    host.parentElement.classList.add('has-note');
    const el = document.createElement('div');
    el.className = 'note';
    el.innerHTML =
      '<div class="nh"><span>line '+n.line+'</span>' +
      (n.inline ? '' : '<span class="warn">summary only</span>') + '</div>' +
      '<div class="nb">'+esc(n.body)+'</div>' +
      (n.suggestion ? '<pre class="sg">'+esc(n.suggestion)+'</pre>' : '') +
      '<div class="na"><button data-e="'+n.id+'">Edit</button>' +
      '<button data-d="'+n.id+'">Delete</button></div>';
    el.addEventListener('click', e=>{
      if(e.target.dataset.e) openEditor(n.block, n.id);
      if(e.target.dataset.d){ notes = notes.filter(x=>x.id!==n.id); save(); drawNotes(); }
    });
    host.appendChild(el);
  });
  drawTray();
}

/* ---------- tray + export ---------- */
function drawTray(){
  $('.tray .n').textContent = notes.length;
  const byFile = {};
  notes.forEach(n => (byFile[n.path] = byFile[n.path] || []).push(n));
  const tb = $('.tray .tb');
  if(!notes.length){
    tb.innerHTML = '<div class="empty">No comments yet. Hover a block and click ' +
      'the + in the gutter, or press c.</div>';
  } else {
    tb.innerHTML = Object.entries(byFile).map(([p, ns]) =>
      '<div class="grp">'+esc(p.split(/[\/\\]/).pop())+'</div>' +
      ns.sort((a,b)=>a.line-b.line).map(n =>
        '<div class="itm" data-go="'+n.id+'"><span class="l'+(n.inline?'':' warn')+'">'+
        n.line+'</span><span class="t">'+esc(n.body)+'</span></div>').join('')
    ).join('');
    tb.querySelectorAll('.itm').forEach(it=>it.addEventListener('click',()=>{
      const n = notes.find(x=>x.id===it.dataset.go);
      const fi = FILES.findIndex(f=>f.path===n.path);
      if(fi !== cur) paint(fi);
      setTimeout(()=>{
        const el = document.querySelector('#b'+n.block);
        if(el) el.scrollIntoView({block:'center', behavior:'smooth'});
      }, 30);
    }));
  }
  document.querySelectorAll('.file-btn .pin').forEach((p,i)=>{
    const c = notes.filter(n=>n.path===FILES[i].path).length;
    p.textContent = c ? c : '';
  });
}

function bodyWithSuggestion(n){
  if(!n.suggestion) return n.body;
  return n.body + '\n\n```suggestion\n' + n.suggestion + '\n```';
}

function payload(){
  const inline = notes.filter(n=>n.inline).map(n=>({
    path: n.path, line: n.line, side: 'RIGHT', body: bodyWithSuggestion(n)
  }));
  const spill = notes.filter(n=>!n.inline);
  let body = REVIEW.summary || 'Markdown review.';
  if(spill.length){
    body += '\n\nComments on lines outside this diff:\n\n' + spill.map(n =>
      '- `' + n.path + ':' + n.line + '` ' + n.body.replace(/\n+/g,' ')).join('\n');
  }
  const out = {body, event:'COMMENT', comments: inline};
  if(REVIEW.commit) out.commit_id = REVIEW.commit;
  return out;
}

function forClaude(){
  const L = ['Post these as a GitHub PR review.', ''];
  if(REVIEW.repo) L.push('Repo: ' + REVIEW.repo);
  L.push('PR: ' + (REVIEW.pr ? '#'+REVIEW.pr : '<fill in>'));
  if(REVIEW.commit) L.push('Head commit: ' + REVIEW.commit);
  L.push('', 'Inline comments (line is in the diff, safe to post as a review comment):');
  const ok = notes.filter(n=>n.inline);
  L.push(...(ok.length ? ok.map(n =>
    '- ' + n.path + ':' + n.line + ' \u2014 ' + n.body.replace(/\n+/g,' ') +
    (n.suggestion ? '  [suggested replacement: ' + JSON.stringify(n.suggestion) + ']' : '')
  ) : ['- none']));
  const bad = notes.filter(n=>!n.inline);
  if(bad.length){
    L.push('', 'Outside the diff, so these must go in the review summary body, ' +
      'not as inline comments (GitHub returns 422 otherwise):');
    L.push(...bad.map(n => '- ' + n.path + ':' + n.line + ' \u2014 ' +
      n.body.replace(/\n+/g,' ')));
  }
  return L.join('\n');
}

function ghCommand(){
  const repo = REVIEW.repo || '<owner>/<repo>';
  const pr = REVIEW.pr || '<pr-number>';
  return "gh api repos/" + repo + "/pulls/" + pr + "/reviews \\\n" +
    "  --method POST --input - <<'JSON'\n" +
    JSON.stringify(payload(), null, 2) + "\nJSON";
}

function toast(msg){
  const t = $('.toast'); t.textContent = msg; t.classList.add('on');
  clearTimeout(t._h); t._h = setTimeout(()=>t.classList.remove('on'), 1900);
}
async function copy(text, label){
  try{ await navigator.clipboard.writeText(text); toast(label + ' copied'); }
  catch(e){
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); ta.remove(); toast(label + ' copied');
  }
}

/* ---------- change navigation ---------- */
let marks = [], at = -1;
function reindex(){
  marks = [...document.querySelectorAll('.blk.added,.blk.changed')]
    .filter(el=>el.offsetParent !== null);
  at = -1;
  $('.pos').textContent = marks.length ? '0/'+marks.length : 'no changes';
}
function jump(step){
  if(!marks.length) return;
  document.querySelectorAll('.blk.target').forEach(e=>e.classList.remove('target'));
  at = (at + step + marks.length) % marks.length;
  marks[at].classList.add('target');
  marks[at].scrollIntoView({block:'center', behavior:'smooth'});
  $('.pos').textContent = (at+1)+'/'+marks.length;
}
function commentAtCursor(){
  const el = marks[at] || [...document.querySelectorAll('.blk:not(.removed)')]
    .find(e=>e.getBoundingClientRect().top > 80);
  if(el) openEditor(+el.id.slice(1));
}

/* ---------- branch selector ---------- */
(async ()=>{
  const wrap = $('#branch-sel-wrap'), sel = $('#branch-sel');
  if(!wrap || !sel) return;
  try {
    const branches = await fetch('/branches').then(r=>r.json());
    if(!branches.length) return;
    const cur = new URLSearchParams(location.search).get('ref') || '';
    branches.forEach(b=>{
      const o = document.createElement('option');
      o.value = b; o.textContent = b;
      if(b === cur) o.selected = true;
      sel.appendChild(o);
    });
    if(cur && !sel.value) {
      const o = document.createElement('option');
      o.value = cur; o.textContent = cur; o.selected = true;
      sel.insertBefore(o, sel.firstChild);
    }
    wrap.style.display = '';
    sel.addEventListener('change', ()=>{
      location.href = '/?ref=' + encodeURIComponent(sel.value);
    });
  } catch(e) { /* server not running or no branches */ }
})();

/* ---------- wire up ---------- */
document.addEventListener('DOMContentLoaded', ()=>{
  load();
  document.querySelectorAll('.file-btn').forEach((b,k)=>
    b.addEventListener('click', ()=>paint(k)));
  $('#t-del').addEventListener('change', e=>{
    document.body.classList.toggle('hide-del', !e.target.checked);
    document.body.classList.toggle('hide-removed', !e.target.checked);
    reindex();
  });
  $('#t-only').addEventListener('change', e=>{
    document.body.classList.toggle('only-changes', e.target.checked);
    reindex();
  });
  $('#prev').addEventListener('click', ()=>jump(-1));
  $('#next').addEventListener('click', ()=>jump(1));
  $('.tray .th').addEventListener('click', ()=>{
    const t = $('.tray'); t.classList.toggle('min');
    $('.tray .chev').textContent = t.classList.contains('min') ? '\u25B2' : '\u25BC';
  });
  $('#x-post').addEventListener('click', async ()=>{
    if(!notes.length) return toast('No comments to post.');
    if(!REVIEW.repo || !REVIEW.pr) return toast('No PR detected — pass --pr to fix.');
    const p = payload();
    const body = {
      repo: REVIEW.repo, pr: REVIEW.pr, commit: REVIEW.commit,
      summary: p.body, comments: p.comments,
    };
    const btn = $('#x-post');
    btn.disabled = true; btn.textContent = 'Posting…';
    try {
      const r = await fetch('/post', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
      const d = await r.json();
      if(r.ok){ toast('Posted to GitHub ✓'); notes=[]; save(); drawNotes(); }
      else toast('Error: ' + d.message);
    } catch(e){ toast('Server not running — use gh command instead.'); }
    btn.disabled = false; btn.textContent = 'Post to GitHub';
  });
  $('#x-gh').addEventListener('click', ()=>copy(ghCommand(), 'gh command'));
  $('#x-clear').addEventListener('click', ()=>{
    if(notes.length && confirm('Delete all '+notes.length+' comments?')){
      notes = []; save(); drawNotes();
    }
  });
  document.addEventListener('keydown', e=>{
    if(e.target.matches('input,textarea')){
      if(e.key==='Escape') closeEditors();
      return;
    }
    if(e.key==='j'||e.key==='n'){ e.preventDefault(); jump(1); }
    if(e.key==='k'||e.key==='p'){ e.preventDefault(); jump(-1); }
    if(e.key==='c'){ e.preventDefault(); commentAtCursor(); }
    if(e.key===']') paint(Math.min(cur+1, FILES.length-1));
    if(e.key==='[') paint(Math.max(cur-1, 0));
    if(e.key==='Escape') closeEditors();
  });
  if(FILES.length) paint(0);
  drawTray();
});
"""

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>__CSS__</style></head>
<body class="__BODY_CLASS__">
<div class="shell">
<aside>
  <div class="meta">
    <h1>__HEADING__</h1>
    <div class="range">__RANGE__</div>
    <div class="tally"><span class="p">+__ADD__</span> <span class="m">-__DEL__</span></div>
    <div class="target-note __TGCLASS__">__TARGET__</div>
  </div>
  <div class="branch-sel-wrap" id="branch-sel-wrap" style="display:none">
    <select id="branch-sel" class="branch-sel"></select>
  </div>
  <div class="files-hd">__NFILES__</div>
  __FILEBTNS__
</aside>
<main>
  <div class="bar">
    <div class="path"></div>
    <label class="tog"><input type="checkbox" id="t-del"> Show removed</label>
    <label class="tog"><input type="checkbox" id="t-only"> Changes only</label>
    <div class="nav">
      <button id="prev" title="Previous change (k)">&uarr;</button>
      <span class="pos"></span>
      <button id="next" title="Next change (j)">&darr;</button>
    </div>
  </div>
  <div class="doc"><div class="doc-inner"></div></div>
</main>
</div>

<div class="tray">
  <div class="th">Review <span class="n">0</span><span class="chev">&#9660;</span></div>
  <div class="tb"></div>
  <div class="tf">
    <button class="btn pri" id="x-post">Post to GitHub</button>
    <button class="btn" id="x-gh">gh command</button>
    <button class="btn danger" id="x-clear">Clear</button>
    <div class="hint">Comments are keyed to this diff and kept in your browser,
      so a refresh or a re-run won't lose them.</div>
  </div>
</div>
<div class="toast"></div>
<script>__JS__</script>
</body></html>
"""


def file_button(f, view_mode=False):
    parts = re.split(r"[/\\]", f["path"])
    name = htmllib.escape(parts[-1])
    dirs = htmllib.escape("/".join(parts[:-1]))
    dir_html = f'<span class="dir">{dirs}/</span>' if dirs else ""
    counts = ('' if view_mode else
              f'<div class="counts"><span class="p">+{f["added"]}</span> '
              f'<span class="m">-{f["removed"]}</span></div>')
    return (f'<button class="file-btn" type="button">'
            f'{dir_html}<span class="fname">{name}</span>'
            f'{counts}<span class="pin"></span></button>')


def build_page(files, heading, range_label, review, target_note, target_ok,
               view_mode=False):
    n = len(files)
    slim = [{k: v for k, v in f.items() if k != "status"} for f in files]
    js = (JS.replace("__FILES__", json.dumps(slim))
            .replace("__REVIEW__", json.dumps(review)))
    body_cls = "hide-del hide-removed" + (" view-mode" if view_mode else "")
    files_label = (f"{n} file{'' if n == 1 else 's'}" if view_mode
                   else f"{n} changed file{'' if n == 1 else 's'}")
    return (PAGE
            .replace("__CSS__", CSS)
            .replace("__JS__", js)
            .replace("__BODY_CLASS__", body_cls)
            .replace("__TITLE__", htmllib.escape(heading))
            .replace("__HEADING__", htmllib.escape(heading))
            .replace("__RANGE__", htmllib.escape(range_label))
            .replace("__ADD__", str(sum(f["added"] for f in files)))
            .replace("__DEL__", str(sum(f["removed"] for f in files)))
            .replace("__TARGET__", target_note)
            .replace("__TGCLASS__", "" if target_ok else "bad")
            .replace("__NFILES__", files_label)
            .replace("__FILEBTNS__", "\n  ".join(
                file_button(f, view_mode) for f in files)))


# ---------------------------------------------------------------- PR discovery

def remote_slug():
    url = git("remote", "get-url", "origin", check=False).strip()
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def detect_pr(branch):
    """Ask gh for the PR number if gh is installed and authenticated."""
    try:
        r = subprocess.run(["gh", "pr", "view", "--json", "number,headRefOid"],
                           capture_output=True, text=True, timeout=15,
                           stdin=subprocess.DEVNULL)
        if r.returncode == 0:
            d = json.loads(r.stdout)
            return d.get("number"), d.get("headRefOid")
    except Exception:
        pass
    return None, None


# ------------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(
        prog="mdreview", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", nargs="?",
                    help="base ref, or A..B / A...B range. Default: HEAD")
    ap.add_argument("--commit", help="review a single commit")
    ap.add_argument("--pr", type=int, help="PR number (auto-detected via gh)")
    ap.add_argument("--summary", default="",
                    help="review summary body used in the exported payload")
    ap.add_argument("--no-breaks", action="store_true",
                    help="treat single newlines as soft wraps, like github.com")
    ap.add_argument("--view", action="store_true",
                    help="render current file content without diff highlighting")
    ap.add_argument("-o", "--out", help="output HTML path")
    ap.add_argument("--no-open", action="store_true",
                    help="write the file but don't open a browser")
    args = ap.parse_args()

    if args.no_breaks and "nl2br" in MD_EXTENSIONS:
        MD_EXTENSIONS.remove("nl2br")

    os.chdir(repo_root())

    branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    slug = remote_slug()
    pr, pr_head = (args.pr, None) if args.pr else detect_pr(branch)
    if args.pr:
        _, pr_head = detect_pr(branch)

    if args.view:
        def render_view(view_ref):
            blob_ref = view_ref or WORKTREE
            paths = all_markdown(ref=view_ref)
            if not paths:
                return None, f"No files found in {view_ref or 'working tree'}."
            files = []
            for path in paths:
                text = read_blob(blob_ref, path) or ""
                if path.endswith(".csv"):
                    blocks = build_csv_blocks(text)
                else:
                    blocks = build_view_blocks(text)
                lines = text.count("\n") + 1 if text else 0
                files.append({"path": path, "status": "M", "blocks": blocks,
                              "added": lines, "removed": 0})
            range_label = view_ref or branch
            commit = git("rev-parse", view_ref or "HEAD", check=False).strip() or None
            ref_label = htmllib.escape(view_ref or branch)
            target = f"Viewing <b>{ref_label}</b>."
            if slug and pr:
                target += f" PR: <b>{htmllib.escape(slug)}#{pr}</b>."
            review = {"key": f"{slug or 'repo'}:view:{range_label}", "repo": slug,
                      "pr": pr, "commit": commit, "summary": args.summary,
                      "branch": branch}
            page = build_page(files, "Planning review", range_label, review,
                              target, True, view_mode=True)
            return page, None

        initial_ref = args.spec
        _, err = render_view(initial_ref)
        if err:
            sys.exit(err)
        print("Serving — Ctrl+C to stop")
        if pr and slug:
            print(f"review target: {slug}#{pr}")
        serve(render_view, no_open=args.no_open)
        return
    else:
        base, head = resolve_refs(args.spec, args.commit)

        if args.commit:
            subject = git("log", "-1", "--format=%s", args.commit).strip()
            short = git("rev-parse", "--short", args.commit).strip()
            heading = subject or f"Commit {short}"
            range_label = f"{short}  ({base}..{head})"
        else:
            heading = "Markdown changes"
            range_label = f"{base} .. " + ("working tree" if head is WORKTREE else head)

        files_meta = changed_markdown(base, head)
        if not files_meta:
            sys.exit(f"No changed Markdown files in {range_label}.")

        stats = numstat(base, head)
        files = []
        for status, path in files_meta:
            old = "" if status == "A" else (read_blob(base, path) or "")
            new = "" if status == "D" else (read_blob(head, path) or "")
            ok_lines = commentable_lines(base, head, path)
            blocks, _, _ = build_file_view(old, new, ok_lines)
            if status == "D":
                blocks = [{"state": "removed", "html": "<p><em>File deleted.</em></p>",
                           "kind": "prose", "line": 1, "end": 1, "side": "LEFT",
                           "anchor": None, "can": False}]
            added, removed = stats.get(path, (0, 0))
            files.append({"path": path, "status": status, "blocks": blocks,
                          "added": added, "removed": removed})

        if head is WORKTREE:
            commit = None
            target_ok = False
            target = ("Reviewing your <b>working tree</b>. Line numbers won't match "
                      "the PR until you push. Re-run against a pushed ref before "
                      "exporting.")
        else:
            commit = git("rev-parse", head).strip()
            target_ok = True
            bits = [f"Anchored to <b>{commit[:9]}</b>."]
            if pr_head and pr_head != commit:
                target_ok = False
                bits.append(f"The PR head is <b>{pr_head[:9]}</b>, so this is not "
                            "the latest push. Comments may land as outdated.")
            target = " ".join(bits)

        if slug and pr:
            target += f" Target: <b>{htmllib.escape(slug)}#{pr}</b>."
        elif slug:
            target += (f" Repo <b>{htmllib.escape(slug)}</b>, no PR found. "
                       "Pass --pr to fill it in.")

        review = {"key": f"{slug or 'repo'}:{range_label}", "repo": slug, "pr": pr,
                  "commit": commit, "summary": args.summary, "branch": branch}
        page = build_page(files, heading, range_label, review, target, target_ok)

    out = args.out
    if not out:
        fd, out = tempfile.mkstemp(prefix="mdreview-", suffix=".html")
        os.close(fd)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(page)

    n_ok = sum(1 for f in files for b in f["blocks"] if b.get("can"))
    print(f"{len(files)} file(s), {n_ok} commentable blocks -> {out}")
    if pr and slug:
        print(f"review target: {slug}#{pr} @ {(commit or 'unpushed')[:9]}")
    if not args.no_open:
        webbrowser.open("file://" + os.path.abspath(out))


if __name__ == "__main__":
    main()
