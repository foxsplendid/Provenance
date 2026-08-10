#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for Provenance parsers + privacy layer. Stdlib unittest, no real data.

Run from repo root:
    python tests/test_provenance.py
    python -m unittest discover -s tests
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # so `import provenance...` works without install

from provenance import redact, common as C, memory as C_mem  # noqa: E402
from provenance.ingest import agents as IA, takeout as IT  # noqa: E402


class TestRedact(unittest.TestCase):
    def test_secrets_removed(self):
        token = "sk-ant-api03-" + "ABCDEFGHIJ1234567890xyz"
        s = f"key {token} mail researcher@example.com ip 192.168.1.9"
        out = redact.redact(s)
        self.assertNotIn("sk-ant-api03", out)
        self.assertNotIn("researcher@example.com", out)
        self.assertNotIn("192.168.1.9", out)
        self.assertIn("[REDACTED:", out)

    def test_versions_preserved(self):
        s = "Claude Code v2.1.9 and python 2026.4.0 build"
        self.assertEqual(redact.redact(s), s)

    def test_more_secret_shapes(self):
        slack_token = "xox" + "b-" + "12345678-abcdefghij"
        cases = {
            slack_token: "[REDACTED:token]",
            "password: hunter2longpw": "password=[REDACTED]",
            "aws_secret_access_key = AbCd1234EfGh5678IjKl": "aws_secret_access_key=[REDACTED]",
            "/home/demo/proj": "/home/<user>/proj",
            "https://user:pass1234@host": "://[REDACTED:creds]@",
        }
        for inp, expect in cases.items():
            self.assertIn(expect, redact.redact(inp), f"{inp!r} -> {redact.redact(inp)!r}")

    def test_ipv4_not_version(self):
        self.assertEqual(redact.redact("released v1.2.3.4 build"), "released v1.2.3.4 build")
        self.assertIn("[REDACTED:ip]", redact.redact("connect to 192.168.0.1 now"))

    def test_json_escape_safety(self):
        # regression: a value-level pass must NOT corrupt JSON escapes (\n) when an
        # email immediately follows a newline.
        obj = {"text": "Researcher,\nanalyst@example.org done\twork"}
        dumped = json.dumps(redact.redact_json(obj), ensure_ascii=False, indent=2)
        reparsed = json.loads(dumped)  # must not raise
        self.assertNotIn("analyst@example.org", reparsed["text"])
        self.assertIn("\n", reparsed["text"])  # newline preserved
        self.assertIn("\t", reparsed["text"])


class TestPseudonymize(unittest.TestCase):
    def test_word_boundary_and_cjk(self):
        amap = {"Alice": "[P1]", "示例研究所": "[O1]"}
        out = redact.pseudonymize("Alice and Alicia at 示例研究所", amap)
        self.assertIn("[P1] and Alicia", out)  # 'Alicia' untouched (word boundary)
        self.assertIn("at [O1]", out)

    def test_cjk_adjacent_ascii_alias(self):
        # regression for C1: ASCII alias touching CJK / inside multiword must mask
        amap = {"Tom": "[P1]", "Real Name": "[P2]"}
        self.assertEqual(redact.pseudonymize("我是Tom的同学", amap), "我是[P1]的同学")
        self.assertEqual(redact.pseudonymize("Real Name是我朋友", amap), "[P2]是我朋友")
        # must NOT over-match
        self.assertEqual(redact.pseudonymize("tomato Tomlinson Tom_Jones", amap),
                         "tomato Tomlinson Tom_Jones")

    def test_sanitize_json_combines(self):
        amap = {"Alice": "[P1]"}
        token = "sk-ant-api03-" + "ABCDEFGHIJ1234567890xyz"
        obj = {"a": f"Alice used {token}"}
        out = redact.sanitize_json(obj, amap)
        self.assertNotIn("Alice", out["a"])
        self.assertNotIn("sk-ant-api03", out["a"])


class TestTakeout(unittest.TestCase):
    def test_parse_ts(self):
        self.assertTrue(IT.parse_ts("2026年6月5日 05:35:51 EDT").startswith("2026-06-05T05:35:51"))
        self.assertTrue(IT.parse_ts("Jun 5, 2026, 4:05:08 PM GMT+8").startswith("2026-06-05T16:05:08"))
        self.assertEqual(IT.parse_ts("nope"), "")

    def test_parse_activity(self):
        html = ('<div class="outer-cell"><div class="content-cell mdl-cell mdl-cell--6-col '
                'mdl-typography--body-1">Prompted hello world<br>2026年6月5日 05:35:51 EDT'
                '<br><p>the answer</p></div><div class="content-cell x">meta</div></div>')
        entries = list(IT.parse_activity(html))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["prompt"], "hello world")
        self.assertTrue(entries[0]["iso"].startswith("2026-06-05"))


class TestAgentParsers(unittest.TestCase):
    def _tmpfile(self, name, lines):
        d = Path(self._td.name)
        p = d / name
        p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines), encoding="utf-8")
        return p

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._td.cleanup()

    def test_claude(self):
        p = self._tmpfile("s.jsonl", [
            {"type": "custom-title", "customTitle": "T", "sessionId": "s"},
            {"type": "user", "timestamp": "2026-01-01T00:00:00Z", "cwd": "D:/x",
             "message": {"role": "user", "content": "do the thing"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:01:00Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"},
                                                          {"type": "tool_use", "name": "Bash"}]}},
        ])
        r = IA.parse_claude(p)
        self.assertEqual(r["name"], "T")
        self.assertEqual([m[1] for m in r["messages"]], ["human", "assistant"])
        self.assertEqual(r["messages"][1][2], "ok")  # tool_use skipped

    def test_codex(self):
        p = self._tmpfile("rollout-x.jsonl", [
            {"type": "session_meta", "timestamp": "2026-01-01T00:00:00Z",
             "payload": {"id": "cx1", "cwd": "D:/y"}},
            {"type": "event_msg", "timestamp": "2026-01-01T00:00:01Z",
             "payload": {"type": "user_message", "message": "fix the bug"}},
            {"type": "event_msg", "timestamp": "2026-01-01T00:00:02Z",
             "payload": {"type": "agent_message", "message": "fixed"}},
            {"type": "response_item", "payload": {"type": "reasoning"}},
        ])
        r = IA.parse_codex(p)
        self.assertEqual(r["session_id"], "cx1")
        self.assertEqual([m[1] for m in r["messages"]], ["human", "assistant"])

    def test_codex_response_item_assistant_message(self):
        p = self._tmpfile("rollout-new.jsonl", [
            {"type": "session_meta", "timestamp": "2026-01-01T00:00:00Z",
             "payload": {"session_id": "cx2", "cwd": "D:/repo"}},
            {"type": "event_msg", "timestamp": "2026-01-01T00:00:01Z",
             "payload": {"type": "user_message", "message": "update the board"}},
            {"type": "response_item", "timestamp": "2026-01-01T00:00:02Z",
             "payload": {"type": "message", "role": "assistant",
                         "content": [{"type": "output_text", "text": "drafted updates"}]}},
        ])
        r = IA.parse_codex(p)
        self.assertEqual(r["session_id"], "cx2")
        self.assertEqual(r["project"], "D:/repo")
        self.assertEqual(r["messages"][-1][2], "drafted updates")

    def test_antigravity(self):
        base = Path(self._td.name) / "brain" / "uuid9" / ".system_generated" / "logs"
        base.mkdir(parents=True)
        p = base / "transcript.jsonl"
        p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in [
            {"type": "USER_INPUT", "created_at": "2026-01-01T00:00:00Z",
             "content": "<USER_REQUEST>build it</USER_REQUEST>"},
            {"type": "RUN_COMMAND", "content": "ls"},
            {"type": "PLANNER_RESPONSE", "created_at": "2026-01-01T00:01:00Z", "content": "done"},
        ]), encoding="utf-8")
        r = IA.parse_antigravity(p)
        self.assertEqual(r["session_id"], "uuid9")
        self.assertEqual(r["messages"][0][2], "build it")  # wrapper stripped, tool skipped
        self.assertEqual([m[1] for m in r["messages"]], ["human", "assistant"])

    def test_cursor(self):
        db = Path(self._td.name) / "state.vscdb"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE cursorDiskKV(key TEXT, value TEXT)")
        comp = {"composerId": "c1", "name": "Chat",
                "createdAt": 1764000000000, "lastUpdatedAt": 1764000600000,
                "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1},
                                                {"bubbleId": "b2", "type": 2}]}
        con.execute("INSERT INTO cursorDiskKV VALUES (?,?)", ("composerData:c1", json.dumps(comp)))
        con.execute("INSERT INTO cursorDiskKV VALUES (?,?)", ("bubbleId:c1:b1", json.dumps({"type": 1, "text": "hi"})))
        con.execute("INSERT INTO cursorDiskKV VALUES (?,?)", ("bubbleId:c1:b2", json.dumps({"type": 2, "text": "yo"})))
        con.commit()
        con.close()
        sessions = list(IA.parse_cursor(db))
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["name"], "Chat")
        self.assertEqual([m[1] for m in sessions[0]["messages"]], ["human", "assistant"])


class TestClassify(unittest.TestCase):
    def test_keyword_and_default(self):
        # force the package generic ruleset (override-independent, stable on CI)
        cats, _label, _order = C.load_categories(C.CATS_FILE)
        cid, _ = C.classify("a machine learning model overview 机器学习", cats)
        self.assertEqual(cid, "02")
        cid2, _ = C.classify("zzz totally unrelated 闲聊", cats)
        self.assertEqual(cid2, cats.get("default", "00"))

    def test_agent_source_in_set(self):
        self.assertIn("cursor", C.AGENT_SOURCES)
        self.assertIn("claude-code", C.AGENT_SOURCES)


class TestEndToEndIngestAgents(unittest.TestCase):
    def test_same_stem_no_collision(self):
        # regression for H1 / 360-vs-348: same UUID stem under two project dirs must
        # NOT overwrite, and index keys must equal file count.
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        for i, proj in enumerate(["projA", "projB"]):
            d = root / "src" / proj
            d.mkdir(parents=True)
            (d / "uuid1.jsonl").write_text(json.dumps(
                {"type": "user", "timestamp": f"2026-01-0{i+1}T00:00:00Z",
                 "message": {"role": "user", "content": f"hello from {proj}"}}) + "\n",
                encoding="utf-8")
        inbox = root / "inbox"
        orig_sources, orig_argv = IA.SOURCES, sys.argv
        IA.SOURCES = {"claude-code": dict(bases=[root / "src"], glob="**/*.jsonl", parse=IA.parse_claude)}
        try:
            sys.argv = ["ingest_agents.py", str(inbox), "claude-code"]
            IA.main()
        finally:
            IA.SOURCES, sys.argv = orig_sources, orig_argv
        files = list((inbox / "claude-code").glob("*.json"))
        idx = json.loads((inbox / "_index.json").read_text(encoding="utf-8"))["conversations"]
        ckeys = [k for k in idx if k.startswith("claude-code:")]
        self.assertEqual(len(files), 2, "same-stem sessions must not overwrite")
        self.assertEqual(len(files), len(ckeys), "index keys must equal file count")


class TestSearchE2E(unittest.TestCase):
    def test_build_query_escape_and_isolation(self):
        from provenance.search import core as S
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        inbox = Path(td.name) / "inbox"
        (inbox / "chatgpt").mkdir(parents=True)
        (inbox / "chatgpt" / "c1.json").write_text(json.dumps(
            {"source": "chatgpt", "conversation_id": "c1", "name": "C++ and data-leak templates",
             "chat_messages": [{"sender": "human", "text": "about C++ templates and data-leak",
                                "created_at": "2026-01-01"}]}, ensure_ascii=False), encoding="utf-8")
        db = Path(td.name) / "idx.db"
        S.build(inbox, db=db)
        self.assertTrue(db.exists())
        self.assertFalse(S.DEFAULT_DB.exists() and S.db_for(inbox) == S.DEFAULT_DB,
                         "test inbox must not use the real DEFAULT_DB")
        import sqlite3
        con = sqlite3.connect(db)
        # plain word matches (tokenizer-agnostic)
        rows = con.execute("SELECT conversation_id FROM conv WHERE conv MATCH ?",
                           (S.fts_query("templates"),)).fetchall()
        self.assertTrue(rows, "escaped plain query should match")
        # reserved FTS5 chars must NOT raise (would be a crash before P0-3)
        for q in ["C++ data-leak", "a-b (foo)", 'x"y AND']:
            con.execute("SELECT 1 FROM conv WHERE conv MATCH ? LIMIT 1", (S.fts_query(q),)).fetchall()
        con.close()


class TestQueryExpansion(unittest.TestCase):
    def test_synonym_bridge(self):
        from provenance.search import core as S
        self.assertIn("机器学习", S.expand_query("ML"))          # abbrev -> CN
        self.assertIn("research gap", S.expand_query("知识缺口"))  # CN -> EN phrase
        self.assertEqual(S.expand_query("zzzznotaterm"), '("zzzznotaterm")')  # unknown: itself


class TestMCP(unittest.TestCase):
    def test_initialize_tools_list_and_notification(self):
        from provenance import mcp_server as M
        r = M.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(r["result"]["serverInfo"]["name"], "provenance")
        self.assertIn("protocolVersion", r["result"])
        tl = M.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in tl["result"]["tools"]}
        self.assertEqual(names, {"search_brain", "get_profile", "list_topics",
                                 "get_portfolio", "get_current_context",
                                 "get_context_capsule"})
        self.assertIsNone(M.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tools_call_get_profile(self):
        import os
        from provenance import mcp_server as M
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        (Path(td.name) / "profile").mkdir()
        (Path(td.name) / "profile" / "01_persona-card.md").write_text("PERSONA CARD BODY", encoding="utf-8")
        old = os.environ.get("PROVENANCE_HOME")
        os.environ["PROVENANCE_HOME"] = td.name
        try:
            r = M.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": "get_profile", "arguments": {}}})
        finally:
            os.environ.pop("PROVENANCE_HOME", None) if old is None else os.environ.__setitem__("PROVENANCE_HOME", old)
        self.assertIn("PERSONA CARD BODY", r["result"]["content"][0]["text"])

    def test_unknown_tool_errors(self):
        from provenance import mcp_server as M
        r = M.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                      "params": {"name": "nope", "arguments": {}}})
        self.assertIn("error", r)


def _make_inbox(root, source="chatgpt", cid="c1", title="notes", turns=("hi",), attachments=None):
    """Write one normalized inbox conversation; returns the inbox dir."""
    d = root / "inbox" / source
    d.mkdir(parents=True, exist_ok=True)
    rec = {"source": source, "conversation_id": cid, "name": title,
           "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z",
           "chat_messages": [{"sender": "human", "text": t, "created_at": "2026-01-01T00:00:00Z"} for t in turns]}
    if attachments:
        rec["attachments"] = attachments
    (d / f"{cid}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return root / "inbox"


class TestPipelineE2E(unittest.TestCase):
    def _run(self, mainfn, *argv):
        old = sys.argv[:]
        sys.argv = ["x", *map(str, argv)]
        try:
            mainfn()
        finally:
            sys.argv = old

    def test_organize_e2e(self):
        from provenance.organize import core as OC
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        _make_inbox(root, "chatgpt", "c1", "machine learning model overview", ("train a model", "overfitting"))
        out = root / "out"
        self._run(OC.main, root / "inbox", out)
        self.assertTrue((out / "00_index.md").exists())
        self.assertIn("c1", (out / "00_catalog.csv").read_text(encoding="utf-8-sig"))

    def test_prep_e2e(self):
        from provenance.organize import prep as PP
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        _make_inbox(root, "chatgpt", "c1", "t", ("question alpha", "question beta"))
        out = root / "prep"
        self._run(PP.main, root / "inbox", out)
        digests = list(out.glob("digest_*.md"))
        self.assertTrue(digests)
        self.assertIn("question alpha", "".join(d.read_text(encoding="utf-8") for d in digests))

    def test_obsidian_attachment_namespacing(self):
        # L11: same-named assets from different sources must not collide / mislink
        from provenance.export import obsidian as OB
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        _make_inbox(root, "chatgpt", "c1", "t1", ("hi",), attachments=["fig.png"])
        _make_inbox(root, "gemini", "g1", "t2", ("yo",), attachments=["fig.png"])
        for src, content in (("chatgpt", b"AAAA"), ("gemini", b"BBBB")):
            ad = root / "inbox" / src / "_assets"; ad.mkdir(parents=True, exist_ok=True)
            (ad / "fig.png").write_bytes(content)
        out = root / "vault"
        self._run(OB.main, root / "inbox", out)
        self.assertEqual((out / "Attachments" / "chatgpt" / "fig.png").read_bytes(), b"AAAA")
        self.assertEqual((out / "Attachments" / "gemini" / "fig.png").read_bytes(), b"BBBB")

    def test_ingest_zip_dedup_e2e(self):
        from provenance.ingest import core as IC
        import zipfile as zf
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        drop = root / "drop"; drop.mkdir()
        inbox = root / "inbox"
        conv = {"conversation_id": "abc", "title": "hello", "chat_messages": [{"sender": "human", "text": "hi"}]}

        def write_zip():
            zp = drop / "provenance__chatgpt__2026-01-01__1conv.zip"
            with zf.ZipFile(zp, "w") as z:
                z.writestr("_export_meta.json", json.dumps({"source": "chatgpt"}))
                z.writestr("abc.json", json.dumps(conv))
        write_zip()
        self._run(IC.main, drop, inbox)
        self.assertTrue((inbox / "chatgpt" / "abc.json").exists())
        write_zip()  # second export of the same conversation
        self._run(IC.main, drop, inbox)
        self.assertEqual(len(list((inbox / "chatgpt").glob("*.json"))), 1)  # dedup by (source, id)


class TestRobustness(unittest.TestCase):
    def test_takeout_iter_leaves_filters(self):  # L7: only matching entries are read
        from provenance.ingest import takeout as TK
        import zipfile as zf
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        z = Path(td.name) / "Takeout.zip"
        with zf.ZipFile(z, "w") as zz:
            zz.writestr("Takeout/My Activity/Gemini Apps/MyActivity.html", "<html>x</html>")
            zz.writestr("Takeout/Drive/huge.bin", b"x" * 1000)
        names = [n for n, _ in TK.iter_leaves(z, lambda n: "Gemini Apps/" in n)]
        self.assertEqual(names, ["Takeout/My Activity/Gemini Apps/MyActivity.html"])

    def test_watch_zip_ready(self):  # L8: partial/garbage zip is not "ready"
        from provenance.ingest import watch as W
        import zipfile as zf
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        good = Path(td.name) / "g.zip"
        with zf.ZipFile(good, "w") as z:
            z.writestr("a.txt", "hi")
        self.assertTrue(W.zip_ready(good))
        bad = Path(td.name) / "b.zip"
        bad.write_bytes(b"PK\x03\x04 truncated not a real zip")
        self.assertFalse(W.zip_ready(bad))

    def test_watch_help_exits_before_starting_loop(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from provenance.ingest import watch as W

        old_argv = sys.argv[:]
        sys.argv = ["prov-watch", "--help"]
        try:
            with redirect_stdout(StringIO()) as stdout:
                with self.assertRaises(SystemExit) as raised:
                    W.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("drop-folder watcher", stdout.getvalue())

    def test_aliases_fail_open_warns(self):  # L5: corrupt alias map warns, not silent
        import io as _io
        import contextlib
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        p = Path(td.name) / "aliases.json"
        p.write_text("{ this is not valid json", encoding="utf-8")
        err = _io.StringIO()
        with contextlib.redirect_stderr(err):
            amap = redact.load_aliases(p)
        self.assertEqual(amap, {})
        self.assertIn("WARNING", err.getvalue())


# --- Phase A: memory hub (library KB + project portfolio) -----------------

def _run_main(mainfn, *argv):
    """Invoke a module main() with a synthetic argv, then restore it."""
    old = sys.argv[:]
    sys.argv = ["x", *map(str, argv)]
    try:
        return mainfn()
    finally:
        sys.argv = old


PROJECT_MD = """---
schema_version: project/1.0
project_id: synthetic-xq17-calibration
title: Synthetic XQ-17 Calibration
status: active
stage: analysis
priority: high
next_actions: [Review synthetic benchmark results, Complete synthetic ablation]
blocked_by: ""
linked_literature: [synthetic-xq17-calibration/reference, DEMOXQ17]
linked_repo: C:/ScriptoriumDemo/projects/synthetic-xq17-calibration
linked_conversations: synthetic-xq17-calibration
updated: 2026-06-11
---

Running log: replied to R2, started ablation.
"""

LIBRARY_KB = {
    "schema_version": "library-kb/1.0",
    "generated_by": "steward 0.1.0",
    "items": [
        {"key": "DEMOXQ17", "title": "Synthetic calibration benchmark",
         "authors": ["Example Author"], "year": "2018", "doi": "10.0000/scriptorium-demo-xq17",
         "folders": ["synthetic-xq17-calibration/baselines"], "tags": ["demo:calibration"], "aiTags": ["calibration"],
         "tldr": "Synthetic observations support the demo calibration.", "readStatus": "To Read",
         "abstract": "Synthetic calibration benchmark abstract.",
         "pdfPaths": ["C:\\Users\\demo-user\\ReferenceLibrary\\demo-xq17.pdf"]},
    ],
}

LIBRARY_KB_11 = {
    **LIBRARY_KB,
    "schema_version": "library-kb/1.1",
    "generated_by": "steward 0.2.0",
    "items": [{**LIBRARY_KB["items"][0], "citekey": "Example Author2018"}],
}


class TestFrontmatter(unittest.TestCase):
    def test_project_frontmatter(self):
        fm, body = C_mem.parse_frontmatter(PROJECT_MD)
        self.assertEqual(fm["project_id"], "synthetic-xq17-calibration")
        self.assertEqual(fm["status"], "active")
        self.assertEqual(fm["next_actions"], ["Review synthetic benchmark results", "Complete synthetic ablation"])
        self.assertEqual(fm["blocked_by"], "")          # quoted empty string
        self.assertIn("DEMOXQ17", fm["linked_literature"])
        self.assertIn("Running log", body)

    def test_block_map(self):
        md = ("---\nkey_metrics:\n  T_R2: 0.80\n  T_RMSE: 85.2\n"
              "  cv: 10-fold stratified\n---\nbody")
        fm, _ = C_mem.parse_frontmatter(md)
        self.assertEqual(fm["key_metrics"]["T_R2"], 0.80)            # number coercion
        self.assertEqual(fm["key_metrics"]["cv"], "10-fold stratified")

    def test_flow_map_and_absent(self):
        fm, _ = C_mem.parse_frontmatter('---\nkey_metrics: {T_R2: 0.8, cv: "10-fold"}\n---\nx')
        self.assertEqual(fm["key_metrics"], {"T_R2": 0.8, "cv": "10-fold"})
        self.assertEqual(C_mem.parse_frontmatter("no frontmatter here")[0], {})


class TestLibraryIngest(unittest.TestCase):
    def test_ingests_10_and_sanitizes(self):
        from provenance.ingest import library as ILib
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        src = root / "library.json"
        src.write_text(json.dumps(LIBRARY_KB, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(_run_main(ILib.main, src, root / "memory"), 0)
        items = C_mem.load_library(root)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["key"], "DEMOXQ17")
        # home username in pdfPaths must be redacted by the privacy layer
        self.assertNotIn("demo-user", json.dumps(items[0], ensure_ascii=False))

    def test_ingests_11_and_preserves_citekey(self):
        from provenance.ingest import library as ILib
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        src = root / "library.json"
        src.write_text(json.dumps(LIBRARY_KB_11, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(_run_main(ILib.main, src, root / "memory"), 0)

        saved = json.loads((root / "memory" / "library.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["schema_version"], "library-kb/1.1")
        self.assertEqual(saved["items"][0]["citekey"], "Example Author2018")

    def test_rejects_wrong_major_and_format(self):
        from provenance.ingest import library as ILib
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        for index, version in enumerate(
                ("library-kb/2.0", "library-kb/1.beta", "library-kb/1.", "not-a-schema")):
            with self.subTest(schema_version=version):
                src = root / f"x-{index}.json"
                memdir = root / f"memory-{index}"
                src.write_text(json.dumps({"schema_version": version, "items": []}), encoding="utf-8")
                self.assertEqual(_run_main(ILib.main, src, memdir), 2)
                self.assertFalse((memdir / C_mem.LIBRARY).exists())

    def test_rejects_legacy_array_without_crashing(self):
        from provenance.ingest import library as ILib
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        src = root / "legacy.json"
        src.write_text(json.dumps(LIBRARY_KB["items"]), encoding="utf-8")
        self.assertEqual(_run_main(ILib.main, src, root / "memory"), 2)
        self.assertFalse((root / "memory" / C_mem.LIBRARY).exists())

    def test_invalid_items_do_not_replace_existing_snapshot(self):
        from provenance.ingest import library as ILib
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        memdir = root / "memory"
        memdir.mkdir()
        existing = {"schema_version": "library-kb/1.0", "items": [{"key": "KEEP0001"}]}
        (memdir / C_mem.LIBRARY).write_text(json.dumps(existing), encoding="utf-8")
        invalid_items = ({}, ["bad"], [{"title": "missing key"}],
                         [{"key": "BAD00001", "title": 7}])
        for index, items in enumerate(invalid_items):
            with self.subTest(items=items):
                src = root / f"broken-{index}.json"
                src.write_text(json.dumps({"schema_version": "library-kb/1.1",
                                           "items": items}), encoding="utf-8")
                self.assertEqual(_run_main(ILib.main, src, memdir), 2)
                saved = json.loads((memdir / C_mem.LIBRARY).read_text(encoding="utf-8"))
                self.assertEqual(saved, existing)


class TestVaultIngest(unittest.TestCase):
    def _vault(self, root):
        v = root / "vault"
        (v / "Projects").mkdir(parents=True)
        (v / "Projects" / "ml.md").write_text(PROJECT_MD, encoding="utf-8")
        (v / "Projects" / "readme.md").write_text("# notes\nno frontmatter here", encoding="utf-8")
        return v

    def test_ingest_projects(self):
        from provenance.ingest import vault as IV
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        _run_main(IV.main, self._vault(root), root / "memory")
        projects = C_mem.load_projects(root)
        self.assertEqual(len(projects), 1)  # non-conforming readme.md skipped
        self.assertIn("synthetic-xq17-calibration", projects)
        self.assertEqual(projects["synthetic-xq17-calibration"]["next_actions"],
                         ["Review synthetic benchmark results", "Complete synthetic ablation"])

    def test_keeps_recent_progress_beyond_the_short_body_excerpt(self):
        from provenance.ingest import vault as IV
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        vault = self._vault(root)
        note = vault / "Projects" / "ml.md"
        note.write_text(
            PROJECT_MD
            + ("Long human context. " * 100)
            + "\n<!-- scriptorium:progress-log:begin -->\n"
            + "<!-- entry:synthetic-session -->\n"
            + "**2100-01-03** - session writeback\n"
            + "- newest synthetic progress\n"
            + "<!-- scriptorium:progress-log:end -->\n",
            encoding="utf-8",
        )

        IV.ingest(vault, root=root, run=True)
        project = C_mem.load_projects(root)["synthetic-xq17-calibration"]

        self.assertNotIn("newest synthetic progress", project["body"])
        self.assertIn("newest synthetic progress", project["_progress_log"])

    def test_progress_excerpt_keeps_newest_complete_entries(self):
        from provenance.ingest import vault as IV

        newest = (
            "\n<!-- entry:newest -->\n"
            "**2100-01-03** - session writeback\n"
            "- newest synthetic progress\n"
        )
        old_blocks = "".join(
            "<!-- entry:old-{index} -->\n"
            "**2099-01-01** - session writeback\n"
            "- old synthetic progress {index} {padding}\n".format(
                index=index,
                padding="x" * 500,
            )
            for index in range(10)
        )
        excerpt = IV._progress_excerpt(
            IV.PROGRESS_BEGIN + newest + old_blocks + IV.PROGRESS_END
        )

        self.assertLessEqual(
            len(excerpt) - len(IV.PROGRESS_BEGIN) - len(IV.PROGRESS_END),
            IV.PROGRESS_CHARS,
        )
        self.assertIn("<!-- entry:newest -->", excerpt)
        self.assertIn("newest synthetic progress", excerpt)
        self.assertNotIn("<!-- entry:old-9 -->", excerpt)
        self.assertTrue(excerpt.rstrip().endswith(IV.PROGRESS_END))


class TestSearchLiterature(unittest.TestCase):
    def test_library_indexed_alongside_conversations(self):
        from provenance.search import core as S
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "inbox" / "chatgpt").mkdir(parents=True)
        (root / "inbox" / "chatgpt" / "c1.json").write_text(json.dumps(
            {"source": "chatgpt", "conversation_id": "c1", "name": "chat about cats",
             "chat_messages": [{"sender": "human", "text": "cats", "created_at": "2026-01-01"}]},
            ensure_ascii=False), encoding="utf-8")
        (root / "memory").mkdir()
        (root / "memory" / "library.json").write_text(json.dumps(
            {"schema_version": "library-kb/1.0", "items": [
                {"key": "DEMOXQ17", "title": "synthetic calibration",
                 "tldr": "synthetic calibration baseline", "year": "2018"}]},
            ensure_ascii=False), encoding="utf-8")
        db = root / "idx.db"
        S.build(root / "inbox", db=db)
        hits = S.query("calibration", db=db)
        self.assertTrue(any(h["conversation_id"] == "lit:DEMOXQ17" for h in hits))
        self.assertTrue(any(h["source"] == "literature" for h in hits))


class TestMCPPortfolio(unittest.TestCase):
    def _with_home(self, root, fn):
        import os
        old = os.environ.get("PROVENANCE_HOME")
        os.environ["PROVENANCE_HOME"] = str(root)
        try:
            return fn()
        finally:
            os.environ.pop("PROVENANCE_HOME", None) if old is None else os.environ.__setitem__("PROVENANCE_HOME", old)

    def _call(self, name, arguments=None):
        from provenance import mcp_server as Mcp
        r = Mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": name, "arguments": arguments or {}}})
        return r["result"]["content"][0]["text"]

    def test_portfolio_and_context(self):
        from provenance.ingest import vault as IV, library as ILib
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        vault = root / "vault"
        (vault / "Projects").mkdir(parents=True)
        (vault / "Projects" / "ml.md").write_text(PROJECT_MD, encoding="utf-8")
        libsrc = root / "library.json"
        libsrc.write_text(json.dumps(LIBRARY_KB, ensure_ascii=False), encoding="utf-8")
        _run_main(IV.main, vault, root / "memory")
        _run_main(ILib.main, libsrc, root / "memory")

        pf = self._with_home(root, lambda: self._call("get_portfolio"))
        self.assertIn("synthetic-xq17-calibration", pf)
        self.assertIn("analysis", pf)         # stage surfaced

        ctx = self._with_home(root, lambda: self._call("get_current_context",
                                                       {"project_id": "synthetic-xq17-calibration"}))
        self.assertIn("Synthetic calibration benchmark", ctx)  # literature matched (by key)

    def test_unknown_project(self):
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "memory").mkdir()
        (root / "memory" / "projects.json").write_text(
            json.dumps({"projects": {}}), encoding="utf-8")
        out = self._with_home(root, lambda: self._call("get_current_context", {"project_id": "nope"}))
        self.assertIn("No project", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
