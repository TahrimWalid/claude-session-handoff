import contextlib
import io
import json
import os
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claude_handoff as ch  # noqa: E402

WIN_ROOT = r"C:\Users\Asiakas\codes\AgentWebUI-Workspace"
SID = "157ce8e3-a522-43f7-8720-b7fe0a917b0f"


def rec(**kw):
    return json.dumps(kw, ensure_ascii=False, separators=(",", ":"))


def windows_transcript(sid=SID):
    """A small transcript shaped like one recorded by Claude Code on Windows."""
    lines = [
        rec(type="summary", summary="Project context review", leafUuid="x"),
        rec(type="user", cwd=WIN_ROOT, sessionId=sid, uuid="u1",
            message={"role": "user", "content": "Review the project context please"}),
        rec(type="assistant", cwd=WIN_ROOT, sessionId=sid, uuid="a1", message={
            "role": "assistant", "content": [
                {"type": "thinking", "thinking": f"Look in {WIN_ROOT}\\src first", "signature": "SIG=="},
                {"type": "tool_use", "id": "t1", "name": "Read",
                 "input": {"file_path": WIN_ROOT + r"\src\app\main.py"}},
                {"type": "tool_use", "id": "t2", "name": "Bash",
                 "input": {"command": "cd /c/Users/Asiakas/codes/AgentWebUI-Workspace/src && ls"}},
            ]}),
        rec(type="user", cwd=WIN_ROOT + r"\src", sessionId=sid, uuid="u2", message={
            "role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                         "content": "see c:/users/asiakas/codes/AgentWebUI-Workspace/README.md "
                                                    r"and C:\Users\Asiakas\codes\AgentWebUI-Workspace-old\x.py "
                                                    r"and C:\Users\Asiakas\.claude\settings.json"}]}),
        rec(type="file-history-snapshot", snapshot={"trackedFileBackups": {
            WIN_ROOT + r"\src\app\main.py": {"backupFileName": "abc@v1"}}}),
        "{this line is broken",
    ]
    data = "\ufeff" + "\r\n".join(lines) + "\r\n"
    return data.encode("utf-8")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ch.main(argv)
    return code, out.getvalue(), err.getvalue()


def load(path):
    return [json.loads(l) if l.strip().startswith("{") and not l.startswith("{this") else l
            for l in path.read_text(encoding="utf-8").splitlines()]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def old(self, path):
        t = time.time() - 3600
        os.utime(path, (t, t))


class SlugTests(unittest.TestCase):
    def test_posix(self):
        self.assertEqual(ch.slugify("/home/admin/my_proj.v2"), "-home-admin-my-proj-v2")

    def test_windows(self):
        self.assertEqual(ch.slugify(r"C:\Users\optimus_3.0\codes\in-gen"),
                         "C--Users-optimus-3-0-codes-in-gen")

    def test_astral_char_is_two_code_units(self):
        self.assertEqual(ch.slugify("/a/😀"), "-a---")

    def test_normalize_root(self):
        self.assertEqual(ch.normalize_root("c:/Users//x/"), r"C:\Users\x")
        self.assertEqual(ch.normalize_root("/home//x/"), "/home/x")
        with self.assertRaises(ch.HandoffError):
            ch.normalize_root("/")
        with self.assertRaises(ch.HandoffError):
            ch.normalize_root("C:\\")


class MapperTests(unittest.TestCase):
    def setUp(self):
        self.m = ch.PathMapper([(WIN_ROOT, "/home/admin/projects/AgentWebUI-Workspace")])

    def test_variants_and_tail(self):
        dst = "/home/admin/projects/AgentWebUI-Workspace"
        self.assertEqual(self.m(WIN_ROOT + r"\src\a.py"), dst + "/src/a.py")
        self.assertEqual(self.m("c:/users/asiakas/codes/agentwebui-workspace/x"), dst + "/x")
        self.assertEqual(self.m("cd /c/Users/Asiakas/codes/AgentWebUI-Workspace && ls"), f"cd {dst} && ls")
        self.assertEqual(self.m("/mnt/c/Users/Asiakas/codes/AgentWebUI-Workspace/y"), dst + "/y")
        self.assertEqual(self.m(WIN_ROOT + "\\"), dst + "/")

    def test_does_not_touch_siblings(self):
        s = WIN_ROOT + r"-old\x.py"
        self.assertEqual(self.m(s), s)

    def test_posix_to_windows(self):
        m = ch.PathMapper([("/home/a/p", r"C:\work\p")])
        self.assertEqual(m("/home/a/p/src/x.py"), r"C:\work\p\src\x.py")
        self.assertEqual(m("~/home/a/p"), "~/home/a/p")
        self.assertEqual(m("/home/a/proj"), "/home/a/proj")

    def test_longest_root_wins(self):
        m = ch.PathMapper([("/a", "/X"), ("/a/b", "/Y")])
        self.assertEqual(m("/a/b/c and /a/z"), "/Y/c and /X/z")


class ImportTests(Base):
    def make_windows_drop(self):
        drop = self.tmp / "drop"
        drop.mkdir()
        t = drop / f"{SID}.jsonl"
        t.write_bytes(windows_transcript())
        side = drop / SID / "subagents"
        side.mkdir(parents=True)
        (side / "agent-1.jsonl").write_text(
            rec(type="user", cwd=WIN_ROOT, sessionId=SID, message={"role": "user", "content": "sub"}) + "\n")
        (drop / SID / "tool-results").mkdir()
        (drop / SID / "tool-results" / "out.txt").write_text("raw output")
        self.old(t)
        return t

    def test_windows_to_linux(self):
        t = self.make_windows_drop()
        cfg = self.tmp / "cfg"
        tgt = "/opt/handoff/AgentWebUI-Workspace"  # a Linux path on any host
        code, out, err = run(["import", str(t), "--to-cwd", tgt, "--config-dir", str(cfg)])
        self.assertEqual(code, 0, err)
        dst = cfg / "projects" / ch.slugify(tgt) / f"{SID}.jsonl"
        self.assertTrue(dst.is_file())
        text = dst.read_text(encoding="utf-8")
        recs = load(dst)
        self.assertEqual(len(recs), 6)
        self.assertEqual(recs[1]["cwd"], tgt)
        self.assertEqual(recs[3]["cwd"], tgt + "/src")
        tools = recs[2]["message"]["content"]
        self.assertIn("SIG==", json.dumps(tools[0]))
        self.assertIn(WIN_ROOT, tools[0]["thinking"])  # signed block untouched
        self.assertEqual(tools[1]["input"]["file_path"], tgt + "/src/app/main.py")
        self.assertEqual(tools[2]["input"]["command"], f"cd {tgt}/src && ls")
        result = recs[3]["message"]["content"][0]["content"]
        self.assertIn(tgt + "/README.md", result)
        self.assertIn(r"AgentWebUI-Workspace-old\x.py", result)  # sibling left alone
        self.assertIn(tgt + "/src/app/main.py", recs[4]["snapshot"]["trackedFileBackups"])
        self.assertEqual(recs[5], "{this line is broken")  # copied verbatim
        self.assertNotIn("\r", text)
        self.assertFalse(text.startswith("\ufeff"))
        sub = cfg / "projects" / ch.slugify(tgt) / SID / "subagents" / "agent-1.jsonl"
        self.assertEqual(json.loads(sub.read_text(encoding="utf-8"))["cwd"], tgt)
        self.assertTrue((sub.parent.parent / "tool-results" / "out.txt").is_file())
        self.assertIn("thinking block", out)
        self.assertIn(r"C:\Users\Asiakas\.claude", err)  # reported foreign path
        self.assertGreater(dst.stat().st_mtime, time.time() - 60)  # fresh mtime
        if os.name != "nt":
            self.assertEqual(dst.stat().st_mode & 0o777, 0o600)

    def test_extra_map(self):
        t = self.make_windows_drop()
        cfg = self.tmp / "cfg"
        code, _, err = run(["import", str(t), "--to-cwd", "/opt/handoff/p", "--config-dir", str(cfg),
                            "--map", r"C:\Users\Asiakas=/home/admin"])
        self.assertEqual(code, 0, err)
        dst = next((cfg / "projects").glob("*/*.jsonl"))
        self.assertIn("/home/admin/.claude/settings.json", dst.read_text())

    def test_needs_to_cwd_for_foreign_path(self):
        t = self.make_windows_drop()
        code, _, err = run(["import", str(t), "--config-dir", str(self.tmp / "cfg")])
        self.assertEqual(code, 1)
        self.assertIn("--to-cwd", err)

    def test_dry_run_writes_nothing(self):
        t = self.make_windows_drop()
        cfg = self.tmp / "cfg"
        code, out, err = run(["import", str(t), "--to-cwd", "/srv/x", "--config-dir", str(cfg), "--dry-run"])
        self.assertEqual(code, 0, err)
        self.assertIn("Dry run", out)
        self.assertFalse(cfg.exists())

    def test_collision_and_force(self):
        t = self.make_windows_drop()
        cfg = self.tmp / "cfg"
        base = ["import", str(t), "--to-cwd", "/srv/x", "--config-dir", str(cfg)]
        self.assertEqual(run(base)[0], 0)
        code, _, err = run(base)
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        code, out, err = run(base + ["--force"])
        self.assertEqual(code, 0, err)
        folder = cfg / "projects" / "-srv-x"
        self.assertEqual(len(list(folder.glob("*.jsonl.bak-*"))), 1)
        self.assertEqual(len(list(folder.glob("*.jsonl"))), 1)
        self.assertEqual(len([p for p in folder.iterdir() if p.name.startswith(".handoff-")]), 0)


class AccountHandoffTests(Base):
    def setUp(self):
        super().setUp()
        self.project = self.tmp / "repo"
        self.project.mkdir()
        self.cwd = os.path.realpath(self.project)
        self.a = self.tmp / ".claude"
        self.b = self.tmp / ".claude2"
        folder = self.a / "projects" / ch.slugify(self.cwd)
        (folder / SID / "subagents").mkdir(parents=True)
        t = folder / f"{SID}.jsonl"
        t.write_text("\n".join([
            rec(type="user", cwd=self.cwd, sessionId=SID, message={"role": "user", "content": "hello there"}),
            rec(type="custom-title", customTitle="Sprint 1", sessionId=SID),
        ]) + "\n")
        (folder / SID / "subagents" / "a.jsonl").write_text(rec(type="user", sessionId=SID, cwd=self.cwd) + "\n")
        (self.a / "file-history" / SID).mkdir(parents=True)
        (self.a / "file-history" / SID / "abc@v1").write_text("old content")
        self.old(t)
        self.t = t

    def test_prefix_handoff_with_history(self):
        code, out, err = run(["import", SID[:8], "--from-config-dir", str(self.a), "--config-dir", str(self.b)])
        self.assertEqual(code, 0, err)
        dst = self.b / "projects" / ch.slugify(self.cwd) / f"{SID}.jsonl"
        self.assertEqual(dst.read_text(), self.t.read_text())  # nothing to rewrite
        self.assertTrue((self.b / "file-history" / SID / "abc@v1").is_file())
        self.assertIn("CLAUDE_CONFIG_DIR=", out)

    def test_new_id_fork(self):
        code, out, err = run(["import", SID, "--from-config-dir", str(self.a),
                              "--config-dir", str(self.b), "--new-id"])
        self.assertEqual(code, 0, err)
        files = list((self.b / "projects" / ch.slugify(self.cwd)).glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        new_id = files[0].stem
        self.assertNotEqual(new_id, SID)
        self.assertTrue(ch.SESSION_ID_RE.match(new_id))
        for line in files[0].read_text().splitlines():
            self.assertEqual(json.loads(line)["sessionId"], new_id)
        sub = files[0].with_suffix("") / "subagents" / "a.jsonl"
        self.assertEqual(json.loads(sub.read_text(encoding="utf-8"))["sessionId"], new_id)
        self.assertTrue((self.b / "file-history" / new_id).is_dir())

    def test_same_place_refused(self):
        code, _, err = run(["import", SID, "--from-config-dir", str(self.a), "--config-dir", str(self.a)])
        self.assertEqual(code, 1)
        self.assertIn("same file", err)

    @unittest.skipIf(os.name == "nt", "symlinks need extra privileges on Windows")
    def test_symlinked_projects_refused(self):
        self.b.mkdir()
        os.symlink(self.a / "projects", self.b / "projects")
        code, _, err = run(["import", SID, "--from-config-dir", str(self.a), "--config-dir", str(self.b)])
        self.assertEqual(code, 1)
        self.assertIn("same file", err)

    def test_list(self):
        code, out, err = run(["list", "--config-dir", str(self.a)])
        self.assertEqual(code, 0, err)
        self.assertIn(SID, out)
        self.assertIn("Sprint 1", out)
        code, out, _ = run(["list", "--config-dir", str(self.a), "--json"])
        data = json.loads(out)
        self.assertEqual(data[0]["first_prompt"], "hello there")
        self.assertEqual(data[0]["cwd"], self.cwd)

    def test_active_warning(self):
        os.utime(self.t, None)
        code, _, err = run(["import", SID, "--from-config-dir", str(self.a),
                            "--config-dir", str(self.b), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("exit it first", err)

    def test_old_transcript_cleanup_warning(self):
        t = time.time() - 40 * 86400
        os.utime(self.t, (t, t))
        code, _, err = run(["import", SID, "--from-config-dir", str(self.a),
                            "--config-dir", str(self.b), "--keep-mtime"])
        self.assertEqual(code, 0)
        self.assertIn("cleanupPeriodDays", err)


class BundleTests(Base):
    def test_pack_then_import(self):
        project = self.tmp / "proj"
        project.mkdir()
        cwd = os.path.realpath(project)
        src = self.tmp / "a"
        folder = src / "projects" / ch.slugify(cwd)
        (folder / SID).mkdir(parents=True)
        t = folder / f"{SID}.jsonl"
        t.write_text(rec(type="user", cwd=cwd, sessionId=SID, message={"role": "user", "content": "hi"}) + "\n")
        (folder / SID / "note.txt").write_text("x")
        self.old(t)
        bundle = self.tmp / "s.tar.gz"
        code, out, err = run(["pack", SID[:6], "--config-dir", str(src), "-o", str(bundle)])
        self.assertEqual(code, 0, err)
        with tarfile.open(bundle) as tar:
            names = tar.getnames()
        self.assertIn("manifest.json", names)
        self.assertIn(f"session/{SID}.jsonl", names)
        dst = self.tmp / "b"
        code, out, err = run(["import", str(bundle), "--to-cwd", "/srv/new/proj", "--config-dir", str(dst)])
        self.assertEqual(code, 0, err)
        imported = dst / "projects" / "-srv-new-proj" / f"{SID}.jsonl"
        self.assertEqual(json.loads(imported.read_text())["cwd"], "/srv/new/proj")
        self.assertTrue((imported.with_suffix("") / "note.txt").is_file())

    def test_unsafe_bundle_rejected(self):
        bad = self.tmp / "bad.tar.gz"
        payload = self.tmp / "p.txt"
        payload.write_text("x")
        with tarfile.open(bad, "w:gz") as tar:
            tar.add(payload, arcname="../../escape.txt")
        code, _, err = run(["import", str(bad), "--to-cwd", "/srv/x", "--config-dir", str(self.tmp / "c")])
        self.assertEqual(code, 1)
        self.assertIn("unsafe", err)
        self.assertFalse((self.tmp.parent / "escape.txt").exists())


@unittest.skipIf(os.name == "nt", "the test folder name exceeds MAX_PATH on default Windows setups")
class LongPathTests(Base):
    def test_long_path_needs_existing_folder(self):
        long_cwd = "/srv/" + "/".join(["averyveryverylongfoldername"] * 9)
        self.assertGreater(len(ch.slugify(long_cwd)), 200)
        cfg = self.tmp / "cfg"
        with self.assertRaises(ch.HandoffError):
            ch.resolve_project_dir(cfg, long_cwd)
        existing = cfg / "projects" / (ch.slugify(long_cwd)[:200] + "-abc123")
        existing.mkdir(parents=True)
        (existing / "x.jsonl").write_text(rec(type="user", cwd=long_cwd) + "\n")
        self.assertEqual(ch.resolve_project_dir(cfg, long_cwd), existing)


# ---------------------------------------------------------------------------
# Every direction between Linux, macOS and Windows
# ---------------------------------------------------------------------------

OS_ROOTS = {
    # (where a session was recorded, where it moves to)
    "linux": ("/home/alice/code/app", "/home/bob/dev/app"),
    "mac": ("/Users/alice/code/app", "/Users/bob/dev/app"),
    "windows": (r"C:\Users\alice\code\app", r"C:\work\app"),
}


def join(root, *parts):
    sep = "\\" if ch.is_windows_style(root) else "/"
    return sep.join((root,) + parts)


def shell_form(root):
    """How the root appears inside a shell command on that OS."""
    if ch.is_windows_style(root):  # Claude Code on Windows runs commands in Git Bash
        return "/" + root[0].lower() + root[2:].replace("\\", "/")
    return root


def os_transcript(root, sid=SID):
    return "\n".join([
        rec(type="user", cwd=root, sessionId=sid, uuid="u1",
            message={"role": "user", "content": "start"}),
        rec(type="assistant", cwd=root, sessionId=sid, uuid="a1", message={
            "role": "assistant", "content": [
                {"type": "thinking", "thinking": "plan around " + root, "signature": "S"},
                {"type": "tool_use", "id": "t1", "name": "Read",
                 "input": {"file_path": join(root, "src", "main.py")}},
                {"type": "tool_use", "id": "t2", "name": "Bash",
                 "input": {"command": f"cd {shell_form(root)}/src && ls"}},
            ]}),
        rec(type="user", cwd=join(root, "src"), sessionId=sid, uuid="u2", message={
            "role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                         "content": "see " + join(root, "README.md") + " done"}]}),
    ]) + "\n"


class DirectionMatrixTests(Base):
    def check(self, src_os, dst_os):
        src, dst = OS_ROOTS[src_os][0], OS_ROOTS[dst_os][1]
        t = self.tmp / f"{SID}.jsonl"
        t.write_text(os_transcript(src), encoding="utf-8")
        self.old(t)
        cfg = self.tmp / "cfg"
        code, out, err = run(["import", str(t), "--to-cwd", dst, "--config-dir", str(cfg)])
        self.assertEqual(code, 0, err)
        imported = cfg / "projects" / ch.slugify(dst) / f"{SID}.jsonl"
        self.assertTrue(imported.is_file(), f"missing {imported}")
        recs = [json.loads(l) for l in imported.read_text(encoding="utf-8").splitlines()]

        win = ch.is_windows_style(dst)
        sep = "\\" if win else "/"
        fwd = dst.replace("\\", "/")
        self.assertEqual(recs[0]["cwd"], dst)
        self.assertEqual(recs[2]["cwd"], dst + sep + "src")
        blocks = recs[1]["message"]["content"]
        self.assertEqual(blocks[0]["thinking"], "plan around " + src)      # signed: untouched
        self.assertEqual(blocks[1]["input"]["file_path"], sep.join([dst, "src", "main.py"]))
        # Inside a shell command a Windows target uses forward slashes, which Git Bash accepts.
        self.assertEqual(blocks[2]["input"]["command"], f"cd {fwd if win else dst}/src && ls")
        # Embedded in text: keep the separator style the text already used.
        text_sep = "\\" if (win and ch.is_windows_style(src)) else "/"
        text_root = dst if text_sep == "\\" else fwd
        self.assertEqual(recs[2]["message"]["content"][0]["content"],
                         f"see {text_root}{text_sep}README.md done")
        # Nothing outside the signed block still points at the old machine.
        rest = json.dumps([recs[0], recs[2], blocks[1:]])
        self.assertFalse(ch.PathMapper([(src, src)]).search(rest))


def _add_direction_tests():
    for a in OS_ROOTS:
        for b in OS_ROOTS:
            def test(self, a=a, b=b):
                self.check(a, b)
            setattr(DirectionMatrixTests, f"test_{a}_to_{b}", test)


_add_direction_tests()


class OSSpecificTests(unittest.TestCase):
    def test_macos_paths_are_case_insensitive(self):
        m = ch.PathMapper([("/Users/alice/code/app", "/home/bob/app")])
        self.assertEqual(m("/users/ALICE/Code/app/x.py"), "/home/bob/app/x.py")

    def test_linux_paths_are_case_sensitive(self):
        m = ch.PathMapper([("/home/alice/code/app", "/srv/app")])
        self.assertEqual(m("/home/alice/Code/app/x"), "/home/alice/Code/app/x")

    def test_macos_private_alias(self):
        m = ch.PathMapper([("/private/var/folders/ab/app", "/srv/app")])
        self.assertEqual(m("cd /var/folders/ab/app/src"), "cd /srv/app/src")

    def test_macos_decomposed_unicode(self):
        import unicodedata
        src = "/Users/alice/caf\u00e9/app"
        m = ch.PathMapper([(src, "/srv/app")])
        self.assertEqual(m(unicodedata.normalize("NFD", src) + "/x"), "/srv/app/x")

    def test_windows_to_windows_other_drive_and_user(self):
        m = ch.PathMapper([(r"C:\Users\alice\app", r"D:\Users\bob\app")])
        self.assertEqual(m(r"C:\Users\alice\app\a.py"), r"D:\Users\bob\app\a.py")
        self.assertEqual(m("cd /c/Users/alice/app && ls"), "cd D:/Users/bob/app && ls")

    def test_wsl_source(self):
        m = ch.PathMapper([(r"C:\code\app", "/home/me/app")])
        self.assertEqual(m("ls /mnt/c/code/app/src"), "ls /home/me/app/src")

    def test_unc_paths(self):
        m = ch.PathMapper([(r"\\nas\share\app", "/mnt/nas/app")])
        self.assertEqual(m(r"\\nas\share\app\x.py"), "/mnt/nas/app/x.py")
        self.assertEqual(m("//nas/share/app/y"), "/mnt/nas/app/y")

    def test_home_detection(self):
        self.assertEqual(ch.home_of(r"C:\Users\me\code\x"), r"C:\Users\me")
        self.assertEqual(ch.home_of("/Users/me/x"), "/Users/me")
        self.assertEqual(ch.home_of("/home/me"), "/home/me")
        self.assertIsNone(ch.home_of("/srv/app"))

    def test_mac_to_linux_reports_other_home_paths(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        t = tmp / f"{SID}.jsonl"
        t.write_text(rec(type="user", cwd="/Users/alice/app", sessionId=SID, message={
            "role": "user", "content": "check /Users/alice/.zshrc too"}) + "\n", encoding="utf-8")
        code, _, err = run(["import", str(t), "--to-cwd", "/home/bob/app",
                            "--config-dir", str(tmp / "cfg"), "--dry-run"])
        self.assertEqual(code, 0, err)
        self.assertIn("/Users/alice/.zshrc", err)


class RealFormatRegressionTests(Base):
    """Shapes seen in real Claude Code transcripts that synthetic data missed."""

    def test_type_key_holding_an_object(self):
        # Tool definitions embed JSON schemas with a property literally named "type".
        line = rec(type="attachment", cwd="/home/a/p", attachment={"tools": [{"schema": {
            "properties": {"type": {"description": "kind", "type": "string"}}}}]})
        t = self.tmp / f"{SID}.jsonl"
        t.write_text(line + "\n", encoding="utf-8")
        self.old(t)
        code, _, err = run(["import", str(t), "--to-cwd", "/srv/p", "--config-dir", str(self.tmp / "c")])
        self.assertEqual(code, 0, err)

    def test_prompt_after_system_reminder_block(self):
        r = {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "<system-reminder>timezone</system-reminder>"},
            {"type": "text", "text": "Real question here"}]}}
        self.assertEqual(ch.prompt_text(r), "Real question here")

    def test_where(self):
        folder = self.tmp / "cfg" / "projects" / "-srv-p"
        (folder / SID).mkdir(parents=True)
        (folder / f"{SID}.jsonl").write_text(rec(type="user", cwd="/srv/p") + "\n", encoding="utf-8")
        code, out, err = run(["where", SID[:8], "--config-dir", str(self.tmp / "cfg")])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{SID}.jsonl", out)
        self.assertIn("scp -r", out)


if __name__ == "__main__":
    unittest.main()
