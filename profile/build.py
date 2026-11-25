#!/usr/bin/env python3
"""Build the GitHub profile.

Reads profile/config.toml, fetches live numbers from the GitHub API and writes:

  dist/terminal-dark.svg   dist/terminal-light.svg   animated boot + neofetch
  dist/stack-dark.svg      dist/stack-light.svg      "cat stack.yml" with icons
  README.md                the sections between BEGIN/END markers

Standard library only, so the workflow needs no pip install.

Usage:
  python profile/build.py --icons /path/to/skill-icons/icons
  python profile/build.py --icons ... --fixture stats.json   (offline preview)
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import textwrap
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "profile" / "config.toml"
README = ROOT / "README.md"
DIST = ROOT / "dist"

PRE_WIDTH = 100  # characters per line in README text blocks

# ---------------------------------------------------------------- GitHub API


class GitHub:
    def __init__(self, token: str | None):
        self.token = token

    def _request(self, url: str, data: bytes | None = None):
        req = urllib.request.Request(url, data=data)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "profile-builder")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode()), resp.headers

    def get(self, path: str):
        return self._request(f"https://api.github.com{path}")

    def paginate(self, path: str):
        sep = "&" if "?" in path else "?"
        page, items = 1, []
        while True:
            data, _ = self.get(f"{path}{sep}per_page=100&page={page}")
            items.extend(data)
            if len(data) < 100:
                return items
            page += 1

    def count(self, path: str) -> int:
        """Count list items without downloading them (per_page=1 + Link header)."""
        sep = "&" if "?" in path else "?"
        data, headers = self.get(f"{path}{sep}per_page=1")
        link = headers.get("Link") or ""
        m = re.search(r'[?&]page=(\d+)>; rel="last"', link)
        return int(m.group(1)) if m else len(data)

    def graphql(self, query: str, variables: dict):
        body = json.dumps({"query": query, "variables": variables}).encode()
        data, _ = self._request("https://api.github.com/graphql", body)
        if data.get("errors"):
            raise RuntimeError(data["errors"])
        return data["data"]


CONTRIB_QUERY = """
query($login: String!) {
  user(login: $login) {
    contributionsCollection {
      totalCommitContributions
      totalPullRequestContributions
    }
  }
}"""


def collect(cfg: dict, gh: GitHub) -> dict:
    login = cfg["user"]
    user, _ = gh.get(f"/users/{login}")
    repos = [r for r in gh.paginate(f"/users/{login}/repos?type=owner") if not r["fork"]]

    languages: dict[str, int] = {}
    for r in repos:
        langs, _ = gh.get(f"/repos/{r['full_name']}/languages")
        for name, size in langs.items():
            languages[name] = languages.get(name, 0) + size

    wanted = {p["repo"] for p in cfg.get("project", []) + cfg.get("process", []) if p.get("repo")}
    team = {p["repo"] for p in cfg.get("project", []) if p.get("team")}
    by_name = {r["name"].lower(): r for r in repos}
    repo_stats = {}
    for name in sorted(wanted):
        r = by_name.get(name.lower())
        if not r:
            print(f"warning: repo {name} not found", file=sys.stderr)
            continue
        author = f"?author={login}" if name in team else ""
        try:
            commits = gh.count(f"/repos/{r['full_name']}/commits{author}")
        except urllib.error.HTTPError:  # empty repositories answer 409
            commits = 0
        repo_stats[name] = {
            "url": r["html_url"],
            "pushed": r["pushed_at"][:10],
            "commits": commits,
            "stars": r["stargazers_count"],
        }

    commits_total = 0
    for r in repos:
        try:
            commits_total += gh.count(f"/repos/{r['full_name']}/commits?author={login}")
        except urllib.error.HTTPError:  # empty repositories answer 409
            pass

    commits_year = prs_year = None
    if gh.token:
        try:
            c = gh.graphql(CONTRIB_QUERY, {"login": login})["user"]["contributionsCollection"]
            commits_year = c["totalCommitContributions"]
            prs_year = c["totalPullRequestContributions"]
        except Exception as exc:  # keep going with REST numbers
            print(f"warning: GraphQL failed: {exc}", file=sys.stderr)

    return {
        "repos": user["public_repos"],
        "followers": user["followers"],
        "stars": sum(r["stargazers_count"] for r in repos),
        "commits_total": commits_total,
        "commits_year": commits_year,
        "prs_year": prs_year,
        "languages": languages,
        "repo_stats": repo_stats,
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
    }


# ---------------------------------------------------------------- themes

# Purple and green, matching the contribution snake.
THEMES = {
    "dark": {
        "bg": "#1E1A2B", "bar": "#2A2340", "border": "#3D3359",
        "text": "#E6E1F5", "dim": "#A79FC2", "accent": "#3FB950",
        "accent2": "#A78BFA", "ok": "#3FB950",
        "dots": ["#FF5F57", "#FEBC2E", "#28C840"],
        "blocks": ["#6E40C9", "#8957E5", "#A371F7", "#C297FF",
                   "#196C2E", "#2EA043", "#3FB950", "#56D364"],
        "langs": ["#8957E5", "#2EA043", "#A371F7", "#56D364", "#C297FF", "#7D7396"],
    },
    "light": {
        "bg": "#FFFFFF", "bar": "#F3EEFC", "border": "#D8CCF2",
        "text": "#24203A", "dim": "#615A7A", "accent": "#1A7F37",
        "accent2": "#8250DF", "ok": "#1A7F37",
        "dots": ["#FF5F57", "#FEBC2E", "#28C840"],
        "blocks": ["#5A32A3", "#6E40C9", "#8250DF", "#A475F9",
                   "#116329", "#1A7F37", "#2DA44E", "#4AC26B"],
        "langs": ["#6E40C9", "#1A7F37", "#A475F9", "#2DA44E", "#C297FF", "#8C849F"],
    },
}

# GitHub linguist colours, so the bar matches what people see on repos.
LANG_COLOURS = {
    "TypeScript": "#3178C6", "JavaScript": "#F1E05A", "Python": "#3572A5",
    "Dart": "#00B4AB", "C++": "#F34B7D", "C": "#555555", "Swift": "#F05138",
    "Java": "#B07219", "Kotlin": "#A97BFF", "Go": "#00ADD8", "C#": "#178600",
    "HTML": "#E34C26", "CSS": "#663399", "Shell": "#89E051",
    "PowerShell": "#012456", "CMake": "#DA3434", "Dockerfile": "#384D54",
    "Makefile": "#427819", "Ruby": "#701516", "PLpgSQL": "#336790",
    "Objective-C": "#438EFF", "Jupyter Notebook": "#DA5B0B", "SCSS": "#C6538C",
}

FONT = ("ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,"
        "'Liberation Mono','DejaVu Sans Mono',monospace")
FS = 14          # font size
CW = FS * 0.602  # character width of a typical monospace font
LH = 20          # line height
PAD = 26
BAR_H = 34

LOGOS = {
    # Plain ASCII only: box-drawing characters render unevenly across fonts.
    "initials": [
        " __        __ _____ ",
        " \\ \\      / /|_   _|",
        "  \\ \\ /\\ / /   | |  ",
        "   \\ V  V /    | |  ",
        "    \\_/\\_/     |_|  ",
        "",
        "  cloud  .  devops  ",
        "  networks . linux  ",
    ],
    "clarinet": [
        "       .-.       ",
        "       | |       ",
        "       |_|       ",
        "      [___]      ",
        "       |o|       ",
        "       |o|-o     ",
        "       |o|       ",
        "       |o|-o     ",
        "      [===]      ",
        "       |o|       ",
        "       |o|-o     ",
        "       |o|       ",
        "       |o|       ",
        "      /   \\      ",
        "     /     \\     ",
        "    '-------'    ",
    ],
}


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def text_el(x, y, content, cls="", fill=None, extra=""):
    f = f' fill="{fill}"' if fill else ""
    c = f' class="{cls}"' if cls else ""
    return (f'<text x="{x:.1f}" y="{y:.1f}"{c}{f} xml:space="preserve"{extra}>'
            f"{content}</text>")


def window(width, height, title, t, body, css, label):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{esc(label)}">
<title>{esc(label)}</title>
<style>
text {{ font-family: {FONT}; font-size: {FS}px; white-space: pre; }}
{css}
</style>
<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="10" fill="{t['bg']}" stroke="{t['border']}"/>
<path d="M0.5 10.5a10 10 0 0 1 10-10h{width - 21}a10 10 0 0 1 10 10v{BAR_H - 10}h-{width - 1}z" fill="{t['bar']}"/>
<line x1="0.5" y1="{BAR_H + .5}" x2="{width - .5}" y2="{BAR_H + .5}" stroke="{t['border']}"/>
{''.join(f'<circle cx="{20 + i * 20}" cy="{BAR_H / 2 + .5}" r="6" fill="{c}"/>' for i, c in enumerate(t['dots']))}
<text x="{width / 2}" y="{BAR_H / 2 + 5}" text-anchor="middle" fill="{t['dim']}" style="font-size:13px">{esc(title)}</text>
{body}
</svg>
"""


def fill(template: str, values: dict) -> str | None:
    try:
        out = template.format(**values)
    except KeyError:
        return None
    return None if "None" in out else out


def fmt_num(v):
    return f"{v:,}" if isinstance(v, int) else v


def top_languages(cfg, stats):
    ignore = set(cfg["hero"].get("ignore_languages", []))
    langs = {k: v for k, v in stats["languages"].items() if k not in ignore}
    total = sum(langs.values()) or 1
    ranked = sorted(langs.items(), key=lambda kv: -kv[1])
    n = cfg["hero"].get("top_languages", 5)
    top = [(k, v / total) for k, v in ranked[:n]]
    rest = 1 - sum(p for _, p in top)
    if rest > 0.005:
        top.append(("Other", rest))
    return top


def lang_colour(t, langs, name):
    """GitHub's own language colours, so each language is recognisable."""
    return LANG_COLOURS.get(name, t["langs"][-1])


# ---------------------------------------------------------------- hero SVG


def hero_svg(cfg, stats, theme: str) -> str:
    t = THEMES[theme]
    h = cfg["hero"]
    user, host = cfg["prompt_user"], cfg["prompt_host"]
    values = {k: fmt_num(v) for k, v in stats.items() if not isinstance(v, (dict, list))}

    info = []
    for entry in h["info"]:
        key, template, *fallback = entry
        value = fill(template, values)
        if value is None and fallback:
            value = fill(fallback[0], values)
        if value is not None:
            info.append((key, value))

    logo = LOGOS[h.get("logo", "initials")]
    logo_w = max(len(line) for line in logo)
    langs = top_languages(cfg, stats)

    x0 = PAD
    x1 = PAD + (logo_w + 3) * CW
    heading = f"{user}@{host}"

    # Info block lines: heading, rule, key/value rows, languages, colour blocks.
    key_w = max(len(k) for k, _ in info) + 1
    rows = [("head", heading), ("rule", "-" * len(heading))]
    rows += [("kv", (k, v)) for k, v in info]
    rows.append(("langs", None))
    legend = []
    line = []
    for name, share in langs:
        item = f"{name} {share * 100:.0f}%"
        if line and len("  ".join(x for x, _ in line)) + len(item) + 4 > 40:
            legend.append(line)
            line = []
        line.append((item, lang_colour(t, langs, name)))
    if line:
        legend.append(line)
    rows += [("legend", ln) for ln in legend]
    rows.append(("blank", None))
    rows.append(("blocks", None))

    boot = h["boot"]
    content_lines = 1 + max(len(logo), len(rows)) + 2  # prompt, block, blank, prompt
    lines_total = max(len(boot) + 3, content_lines)
    width = 760
    height = int(BAR_H + PAD + lines_total * LH + PAD - 6)

    def y(i):
        return BAR_H + PAD + 10 + i * LH

    # ---- timeline (seconds)
    step = 0.16
    boot_start = 0.25
    login_at = boot_start + len(boot) * step + 0.25
    clear_at = login_at + 1.1
    type_at = clear_at + 0.35
    cmd = "neofetch"
    out_at = type_at + len(cmd) * 0.07 + 0.3

    css = f"""
.b {{ opacity: 0; animation-name: life; animation-timing-function: linear; animation-fill-mode: both; }}
.s {{ animation: show 0.05s linear both; }}
.c {{ animation: type 0.02s linear both; }}
.cursor {{ animation: blink 1.1s steps(1, end) infinite; }}
@keyframes life {{ 0% {{ opacity: 0; }} 1% {{ opacity: 1; }} 99% {{ opacity: 1; }} 100% {{ opacity: 0; }} }}
@keyframes show {{ 0% {{ opacity: 0; }} 100% {{ opacity: 1; }} }}
@keyframes type {{ 0% {{ fill-opacity: 0; }} 100% {{ fill-opacity: 1; }} }}
@keyframes blink {{ 0% {{ opacity: 1; }} 50% {{ opacity: 0; }} }}
@media (prefers-reduced-motion: reduce) {{ .b, .s, .c, .cursor {{ animation: none; }} }}
"""
    body = []

    def boot_line(i, content, start):
        dur = clear_at - start
        body.append(text_el(x0, y(i), content, "b", t["text"],
                            f' style="animation-delay:{start:.2f}s;animation-duration:{dur:.2f}s"'))

    # ---- phase 1: boot log, then login
    for i, msg in enumerate(boot):
        boot_line(i, f'[<tspan fill="{t["ok"]}">  OK  </tspan>] {esc(msg)}', boot_start + i * step)
    tz = dt.timezone(dt.timedelta(hours=cfg.get("timezone_offset_hours", 0)))
    stamp = dt.datetime.fromisoformat(stats["generated"]).astimezone(tz)
    n = len(boot) + 1
    boot_line(n, f'{esc(cfg["prompt_user"])}-tavasi login: <tspan fill="{t["accent"]}">{user}</tspan>', login_at)
    boot_line(n + 1, f'<tspan fill="{t["dim"]}">Last login: {stamp:%a %b %d %Y} from github.com</tspan>', login_at + 0.4)

    # ---- phase 2: prompt, typed command, neofetch output
    def delay(sec):
        return f' style="animation-delay:{sec:.2f}s"'

    prompt = (f'<tspan fill="{t["accent2"]}">{user}@{host}</tspan>'
              f'<tspan fill="{t["text"]}">:</tspan><tspan fill="{t["accent"]}">~</tspan>'
              f'<tspan fill="{t["text"]}">$ </tspan>')
    typed = "".join(f'<tspan class="c"{delay(type_at + i * 0.07)}>{ch}</tspan>' for i, ch in enumerate(cmd))
    body.append(text_el(x0, y(0), prompt + typed, "s", t["text"], delay(clear_at)))

    for i, line in enumerate(logo):
        body.append(text_el(x0, y(i + 1.6), esc(line), "s", t["accent"], delay(out_at + i * 0.03)))

    bar_w = 40 * CW
    for i, (kind, data) in enumerate(rows):
        yy = y(i + 1.6)
        d = delay(out_at + i * 0.03)
        if kind == "head":
            body.append(text_el(x1, yy, f'<tspan fill="{t["accent"]}">{user}</tspan>@<tspan fill="{t["accent"]}">{host}</tspan>', "s", t["text"], d))
        elif kind == "rule":
            body.append(text_el(x1, yy, esc(data), "s", t["dim"], d))
        elif kind == "kv":
            k, v = data
            body.append(text_el(x1, yy, f'<tspan fill="{t["accent"]}" font-weight="bold">{esc(k + ":"):<{key_w}}</tspan> {esc(v)}', "s", t["text"], d))
        elif kind == "langs":
            body.append(text_el(x1, yy, f'<tspan fill="{t["accent"]}" font-weight="bold">{"Languages:":<{key_w}}</tspan>', "s", t["text"], d))
            bx, by = x1 + (key_w + 1) * CW, yy - 10
            segs = []
            cur = bx
            for name, share in langs:
                w = max(bar_w * share, 2)
                segs.append(f'<rect x="{cur:.1f}" y="{by:.1f}" width="{w:.1f}" height="10" fill="{lang_colour(t, langs, name)}"/>')
                cur += w
            body.append(f'<g class="s"{d}><clipPath id="barclip-{theme}"><rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w:.1f}" height="10" rx="5"/></clipPath>'
                        f'<g clip-path="url(#barclip-{theme})">{"".join(segs)}</g></g>')
        elif kind == "legend":
            parts = "  ".join(f'<tspan fill="{c}">●</tspan> {esc(item)}' for item, c in data)
            body.append(text_el(x1 + (key_w + 1) * CW, yy, parts, "s", t["dim"], d))
        elif kind == "blocks":
            size = 18
            blocks = "".join(f'<rect x="{x1 + j * (size + 4):.1f}" y="{yy - 13:.1f}" width="{size}" height="{size}" rx="3" fill="{c}"/>'
                             for j, c in enumerate(t["blocks"]))
            body.append(f'<g class="s"{d}>{blocks}</g>')

    end = out_at + len(rows) * 0.03 + 0.2
    last = max(len(logo), len(rows)) + 2.2
    body.append(text_el(x0, y(last), prompt + f'<tspan class="cursor" fill="{t["accent"]}">█</tspan>', "s", t["text"], delay(end)))

    label = (f"Terminal: {user}@{host} boots and runs neofetch. "
             + ". ".join(f"{k}: {v}" for k, v in info)
             + ". Top languages: " + ", ".join(f"{n} {p * 100:.0f}%" for n, p in langs) + ".")
    return window(width, height, h.get("title", heading), t, "\n".join(body), css, label)


# ---------------------------------------------------------------- stack SVG


def load_icons(icon_dir: Path) -> dict[str, dict[str, Path]]:
    icons: dict[str, dict[str, Path]] = {}
    for f in icon_dir.glob("*.svg"):
        stem = f.stem
        for suffix, theme in (("-Dark", "dark"), ("-Light", "light")):
            if stem.endswith(suffix):
                icons.setdefault(stem[: -len(suffix)].lower(), {})[theme] = f
                break
        else:
            icons.setdefault(stem.lower(), {})["any"] = f
    return icons


def icon_markup(path: Path, x, y, size, uid):
    svg = path.read_text()
    svg = re.sub(r'id="([^"]+)"', lambda m: f'id="{uid}-{m.group(1)}"', svg)
    svg = re.sub(r'url\(#([^)]+)\)', lambda m: f'url(#{uid}-{m.group(1)})', svg)
    svg = re.sub(r'href="#([^"]+)"', lambda m: f'href="#{uid}-{m.group(1)}"', svg)
    svg = re.sub(r'<svg\b([^>]*?)\swidth="[^"]*"', r"<svg\1", svg, count=1)
    svg = re.sub(r'<svg\b([^>]*?)\sheight="[^"]*"', r"<svg\1", svg, count=1)
    return svg.replace("<svg", f'<svg x="{x:.1f}" y="{y:.1f}" width="{size}" height="{size}"', 1)


def stack_svg(cfg, icons, theme: str) -> str:
    t = THEMES[theme]
    rows = cfg["stack"]
    key_w = max(len(r["key"]) for r in rows) + 2
    size, gap = 38, 9
    width = 760
    x_icons = PAD + key_w * CW
    per_row = int((width - PAD - x_icons + gap) // (size + gap))

    body = []
    y = BAR_H + PAD + 4
    prompt = (f'<tspan fill="{t["accent2"]}">{cfg["prompt_user"]}@{cfg["prompt_host"]}</tspan>'
              f':<tspan fill="{t["accent"]}">~</tspan>$ cat stack.yml')
    body.append(text_el(PAD, y + 10, prompt, "", t["text"]))
    y += LH + 12
    names = []
    for n, row in enumerate(rows):
        key = f'<tspan fill="{t["accent"]}" font-weight="bold">{esc(row["key"])}</tspan>:'
        if "text" in row:
            body.append(text_el(PAD, y + 17, key, "", t["text"]))
            body.append(text_el(x_icons, y + 17, f'[{esc(row["text"])}]', "", t["text"]))
            names.append(f'{row["key"]}: {row["text"]}')
            y += LH + 18
            continue
        body.append(text_el(PAD, y + size / 2 + 5, key, "", t["text"]))
        row_names = []
        for i, name in enumerate(row["icons"]):
            variants = icons.get(name)
            if not variants:
                raise SystemExit(f"unknown icon '{name}' in stack row '{row['key']}'")
            path = variants.get(theme) or variants.get("any")
            col, line = i % per_row, i // per_row
            body.append(icon_markup(path, x_icons + col * (size + gap), y + line * (size + gap),
                                    size, f"{theme}{n}-{i}"))
            row_names.append(name)
        lines = (len(row["icons"]) - 1) // per_row + 1
        names.append(f'{row["key"]}: {", ".join(row_names)}')
        y += lines * (size + gap) + 6
    height = int(y + PAD - 10)
    label = "Tech stack. " + ". ".join(names) + "."
    notice = ("<!-- Icons from skill-icons, Copyright (c) 2022 tandpfun, MIT License: "
              "https://github.com/tandpfun/skill-icons -->")
    return window(width, height, "stack.yml", t, notice + "\n" + "\n".join(body), "", label)


# ---------------------------------------------------------------- README sections


def link(url, label):
    return f'<a href="{esc(url)}">{esc(label)}</a>'


def wrap(text, indent=4):
    return textwrap.wrap(text, PRE_WIDTH, initial_indent=" " * indent,
                         subsequent_indent=" " * indent, break_on_hyphens=False)


def ps_block(cfg, stats):
    rs = stats["repo_stats"]
    out = ["<pre>", "STATUS     LAST UPDATE   PROJECT"]
    for p in cfg.get("process", []):
        s = rs.get(p.get("repo", ""), {})
        pushed = s.get("pushed", "-")
        name = link(s["url"], p["name"]) if s.get("url") else esc(p["name"])
        out.append(f"{p['state']:<10} {pushed:<12}  {name}  <i># {esc(p['note'])}</i>")
    out.append("</pre>")
    return "\n".join(out)


def projects_block(cfg, stats):
    rs = stats["repo_stats"]
    blocks = []
    for p in cfg["project"]:
        if not p.get("featured"):
            continue
        s = rs.get(p["repo"], {})
        lines = ["<pre>", f"<b>$ man {esc(p['command'])}</b>", "", "<b>PROJECT</b>"]
        name = p["title"] + (f" ({p['subtitle']})" if p.get("subtitle") else "")
        lines += [esc(x) for x in wrap(name)]
        lines += ["", "<b>WHAT IT DOES</b>"] + [esc(x) for x in wrap(p["description"])]
        lines += ["", "<b>RESULTS</b>"]
        for item in p["proof"]:
            lines += [esc(x) for x in textwrap.wrap(item, PRE_WIDTH, initial_indent="    + ",
                                                    subsequent_indent="      ")]
        lines += ["", "<b>BUILT WITH</b>"] + [esc(x) for x in wrap(p["stack"])]
        lines += ["", "<b>LINKS</b>"]
        if s:
            who = "commits by me" if p.get("team") else "commits"
            lines.append(f"    {link(s['url'], 'source code')} · {s['commits']:,} {who} · "
                         f"pushed {s['pushed']}")
        lines.append("</pre>")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def more_block(cfg, stats):
    rs = stats["repo_stats"]
    items = [p for p in cfg["project"] if not p.get("featured")]
    w = max(len(p["command"]) for p in items) + 3
    lines = ["<pre>"]
    for p in items:
        s = rs.get(p["repo"], {})
        name = link(s["url"], p["command"]) if s else esc(p["command"])
        lines.append(f"{name}{' ' * (w - len(p['command']))}{esc(p['summary'])}")
        lines.append(f"{' ' * w}{esc(p['stack'])}")
        if p.get("demo"):
            lines.append(f"{' ' * w}{link(p['demo'], 'live demo')}")
    lines.append("</pre>")
    return "\n".join(lines)


def replace_section(text, name, content):
    pattern = re.compile(rf"(<!-- BEGIN {name} -->).*?(<!-- END {name} -->)", re.S)
    if not pattern.search(text):
        raise SystemExit(f"README is missing the {name} markers")
    return pattern.sub(lambda m: f"{m.group(1)}\n{content}\n{m.group(2)}", text)


# ---------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--icons", type=Path, required=True, help="skill-icons icons/ folder")
    ap.add_argument("--fixture", type=Path, help="read stats from JSON instead of the API")
    ap.add_argument("--save-stats", type=Path, help="write the fetched stats to JSON")
    args = ap.parse_args()

    cfg = tomllib.loads(CONFIG.read_text())
    if args.fixture:
        stats = json.loads(args.fixture.read_text())
    else:
        stats = collect(cfg, GitHub(os.environ.get("GITHUB_TOKEN")))
    if args.save_stats:
        args.save_stats.write_text(json.dumps(stats, indent=2))

    DIST.mkdir(exist_ok=True)
    icons = load_icons(args.icons)
    for theme in THEMES:
        (DIST / f"terminal-{theme}.svg").write_text(hero_svg(cfg, stats, theme))
        (DIST / f"stack-{theme}.svg").write_text(stack_svg(cfg, icons, theme))

    text = README.read_text()
    text = replace_section(text, "ps", ps_block(cfg, stats))
    text = replace_section(text, "projects", projects_block(cfg, stats))
    text = replace_section(text, "more-projects", more_block(cfg, stats))
    README.write_text(text)
    print("built", ", ".join(sorted(p.name for p in DIST.glob("*.svg"))))


if __name__ == "__main__":
    main()
