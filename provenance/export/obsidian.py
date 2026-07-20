#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Provenance -> Obsidian vault export (stage 04).

Turns the scrubbed inbox into a navigable Obsidian vault: one note per
conversation (frontmatter + the user's own turns + attachment links + concept
wikilinks), plus Topic/Project MOCs and a Home note. Lean by design — notes hold
the USER turns (the second-brain signal), not full transcripts; the raw JSON path
is in frontmatter. Reads the already-secret-scrubbed inbox; the vault is for
personal local use (not the de-identified profile).

Usage:
    python to_obsidian.py            # ../inbox -> ./vault
    python to_obsidian.py <out>      # custom vault dir
    python to_obsidian.py <inbox> <out>
"""
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from provenance import common as C

ROOT = C.data_root()
DEFAULT_INBOX = ROOT / "inbox"
DEFAULT_OUT = ROOT / "vault"
PROFILE_DIR = ROOT / "profile"
CATS, LABEL, _ORDER = C.load_categories()

# Generic deterministic concept links. Users can tune these terms locally.
CONCEPTS = [
    "研究问题", "假设", "证据", "文献综述", "引用", "知识缺口", "可复现性", "实验设计",
    "数据集", "基线", "消融实验", "数据泄漏", "交叉验证", "可解释性", "机器学习", "深度学习",
    "论文", "汇报", "幻灯片", "知识管理", "Obsidian", "Claude Code", "Codex", "Cursor",
    "Docker", "Git", "Python", "R语言",
]
SLUG_BAD = re.compile(r'[\\/:*?"<>|#\^\[\]\n\r\t]+')


def slug(s, n=32):
    s = SLUG_BAD.sub(" ", s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:n].strip(" .") or "untitled")  # trailing dot/space = invalid on Windows


def clip(s, n=800):
    s = re.sub(r"\n{3,}", "\n\n", s or "").strip()
    return s if len(s) <= n else s[:n] + f" …[+{len(s)-n}]"


def norm_project(p):
    """Collapse generated/timestamped run dirs into one logical project."""
    p = (p or "").replace("\\", "/").rstrip("/").split("/")[-1]
    p = re.sub(r"_\d{8}_\d{6}$", "", p)          # strip trailing _YYYYMMDD_HHMMSS
    if p.startswith("paper_ppt_agent") or p.startswith("paper-ppt"):
        return "paper-ppt-agent"
    return p


def yaml_list(items):
    return "[" + ", ".join(json.dumps(x, ensure_ascii=False) for x in items) + "]"


def main():
    C.cli_help(__doc__)
    args = sys.argv[1:]
    if len(args) == 2:
        inbox, out = Path(args[0]).resolve(), Path(args[1]).resolve()
    elif len(args) == 1:
        inbox, out = DEFAULT_INBOX, Path(args[0]).resolve()
    else:
        inbox, out = DEFAULT_INBOX, DEFAULT_OUT
    if not inbox.exists():
        print(f"No inbox at {inbox}.")
        return

    conv_dir = out / "Conversations"
    moc_dir = out / "MOC"
    asset_dir = out / "Attachments"
    for d in (conv_dir, moc_dir, out / "Profile", asset_dir):
        d.mkdir(parents=True, exist_ok=True)
    IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}

    by_cat, by_project = {}, {}
    count = 0
    for jf in sorted(inbox.glob("*/*.json")):
        if jf.name.startswith("_"):
            continue
        try:
            obj = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            continue
        source = jf.parent.name
        m = C.extract_meta(obj, source, jf.stem)
        cid, cat_label = C.classify_with_fallback(f"{m['title']} {m['first_user']}", source, CATS, LABEL)
        turns = C.user_turns(obj)
        project = norm_project(obj.get("project") or "")
        attachments = obj.get("attachments") if isinstance(obj.get("attachments"), list) else []

        blob = (m["title"] + " " + " ".join(turns)).lower()
        concepts = [c for c in CONCEPTS if c.lower() in blob]

        note_name = f"{m['date'] or '0000'}_{source}_{slug(m['title'])}_{m['conversation_id'][-6:]}.md"
        rel = f"Conversations/{source}/{note_name}"
        (conv_dir / source).mkdir(parents=True, exist_ok=True)

        tags = [f"source/{source}", f"cat/{cid}"]
        if project:
            tags.append(f"project/{slug(project,24).replace(' ', '-')}")
        fm = ["---",
              f"source: {source}",
              f"date: {m['date']}",
              f"category: \"{cid} {cat_label}\"",
              f"conversation_id: {m['conversation_id']}",
              f"turns: {len(turns)}",
              f"tags: {yaml_list(tags)}"]
        if project:
            fm.append(f"project: {json.dumps(project, ensure_ascii=False)}")
        if attachments:
            fm.append(f"attachments: {yaml_list(attachments)}")
        raw_rel = str(jf.relative_to(ROOT)) if ROOT in jf.parents else str(jf)
        fm.append(f"raw: {json.dumps(raw_rel.replace(chr(92), '/'), ensure_ascii=False)}")
        fm.append("---")

        body = [f"# {m['title']}", "",
                f"> [{source}] · {m['date']} · {cat_label} · {len(turns)} user turns" + (f" · project: {project}" if project else ""), ""]
        if concepts:
            body.append("**关联概念：** " + " ".join(f"[[{c}]]" for c in concepts) + "\n")
        if attachments:
            shown = []
            for a in attachments:
                srcfile = inbox / source / "_assets" / a
                # namespace by source so same-named assets from different sources
                # don't collide in the flat vault / link to the wrong file (L11)
                dest = asset_dir / source / a
                if srcfile.exists() and not dest.exists():
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(srcfile, dest)
                    except Exception:
                        pass
                ref = f"Attachments/{source}/{a}"  # path-qualified wikilink (unambiguous)
                shown.append(f"![[{ref}]]" if Path(a).suffix.lower() in IMG_EXT else f"[[{ref}]]")
            body.append("**附件：** " + " ".join(shown) + "\n")
        body.append("## 用户提问")
        body.append("")
        if turns:
            for i, t in enumerate(turns, 1):
                body.append(f"**U{i}.** {clip(t)}\n")
        else:
            body.append("_(no extracted user turns; see raw)_\n")

        (conv_dir / source / note_name).write_text(
            "\n".join(fm) + "\n\n" + "\n".join(body) + "\n", encoding="utf-8")
        count += 1

        by_cat.setdefault((cid, cat_label), []).append((m["date"], rel, m["title"], source))
        if project:
            by_project.setdefault(project, []).append((m["date"], rel, m["title"], source))

    # Topic MOCs
    moc_links_topic = []
    for (cid, cat_label), items in sorted(by_cat.items()):
        fn = f"Topic - {cid} {slug(cat_label,20)}.md"
        lines = [f"# {cid} {cat_label}", "", f"> {len(items)} conversations.", ""]
        for date, rel, title, source in sorted(items):
            lines.append(f"- {date} · [{source}] [[{rel}|{slug(title,60)}]]")
        (moc_dir / fn).write_text("\n".join(lines) + "\n", encoding="utf-8")
        moc_links_topic.append((f"{cid} {cat_label}", len(items), f"MOC/{fn}"))

    # Project MOCs
    moc_links_proj = []
    for project, items in sorted(by_project.items(), key=lambda kv: -len(kv[1])):
        fn = f"Project - {slug(project,48)}.md"
        lines = [f"# {project}", "", f"> {len(items)} agent sessions.", ""]
        for date, rel, title, source in sorted(items):
            lines.append(f"- {date} · [{source}] [[{rel}|{slug(title,60)}]]")
        (moc_dir / fn).write_text("\n".join(lines) + "\n", encoding="utf-8")
        moc_links_proj.append((project, len(items), f"MOC/{fn}"))

    # Profile copy
    prof_links = []
    if PROFILE_DIR.exists():
        for pf in sorted(PROFILE_DIR.glob("*.md")):
            shutil.copy2(pf, out / "Profile" / pf.name)
            prof_links.append(f"Profile/{pf.name}")

    # Home
    home = ["# Provenance — 外置大脑首页", "",
            f"> Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} · {count} conversations · de-identified profile in Profile/.", "",
            "## 画像"]
    home += [f"- [[{p}]]" for p in prof_links] or ["_(run provenance-profile skill first)_"]
    home += ["", "## 主题 MOC"]
    home += [f"- [[{path}|{name}]] ({n})" for name, n, path in moc_links_topic]
    home += ["", "## 项目 MOC（编程 agent）"]
    home += [f"- [[{path}|{name}]] ({n})" for name, n, path in moc_links_proj[:20]]
    home += ["", "## Dataview（装 Dataview 插件后自动生成）", "",
             "```dataview", "TABLE source, category, turns, date",
             "FROM \"Conversations\"", "SORT date DESC", "```", ""]
    (out / "Home.md").write_text("\n".join(home) + "\n", encoding="utf-8")

    print(f"Obsidian vault: {count} notes, {len(moc_links_topic)} topic MOCs, "
          f"{len(moc_links_proj)} project MOCs -> {out}")


if __name__ == "__main__":
    main()
