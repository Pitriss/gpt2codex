#!/usr/bin/env python3
"""Offline queue regression tests. No real GitHub, Git, Codex, or sessions."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).with_name("gpt2codex-github-queue.py")
spec = importlib.util.spec_from_file_location("g2c_github_queue", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FakeGitHub:
    def __init__(self, issues):
        self.values = {x["number"]: x for x in issues}
        self.comments = []
        self.closed = []
        self.private = True

    def validate_repo(self):
        if not self.private:
            raise module.QueueError("public repository")

    def issues(self):
        return iter(self.values.values())

    def issue(self, number):
        return self.values[number]

    def comment(self, number, body):
        self.comments.append((number, body))

    def close(self, number):
        self.closed.append(number)


def make_issue(number=1, login="Pitriss", project="kencoder", mode="read-only", task="List files"):
    return {
        "id": number + 10000,
        "number": number,
        "state": "open",
        "title": f"[g2c] task {number}",
        "body": f"### Projekt\n\n{project}\n\n### Rezim\n\n{mode}\n\n### Zadani\n\n{task}\n",
        "user": {"login": login},
    }


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Tests must never write approvals into the real user's state home.
        self.patch_home = mock.patch.dict(os.environ, {"HOME": str(self.root)})
        self.patch_home.start()
        self.addCleanup(self.patch_home.stop)
        self.patch_state = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "persist")})
        self.patch_state.start()
        self.addCleanup(self.patch_state.stop)
        (self.root / ".tmp").mkdir(mode=0o700)
        (self.root / ".log").mkdir(mode=0o700)
        self.project = self.root / "work"
        self.project.mkdir()
        self.bin = self.root / "mock-codex"
        self.bin.write_text('#!/bin/sh\nprintf "MOCK RESULT\\n"\nexit 0\n')
        self.bin.chmod(0o700)
        self.cfg = {
            "repository": "Pitriss/gpt2codex-queue",
            "allowed_authors": ["Pitriss"],
            "project_roots": {"kencoder": str(self.project)},
            "codex_bin": str(self.bin),
            "worker_hostname": "test-worker",
        }
        self.store = module.Store(self.root)

    def test_state_ignores_codex_temporary_xdg_state_home(self):
        stable = self.root / ".local/state/gpt2codex/github-queue"
        self.assertEqual(self.store.state_dir, stable)
        with self.store.lock():
            obj = self.store.load()
            obj["jobs"]["2"] = {"number": 2, "status": "pending"}
            self.store.save(obj)
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "other-sandbox")}):
            second = module.Store(self.root)
            self.assertEqual(second.state_dir, stable)
            with second.lock():
                self.assertEqual(second.load()["jobs"]["2"]["status"], "pending")
        self.assertFalse((self.root / "persist/gpt2codex/github-queue/state.json").exists())

    def test_init_config_preserves_existing_config_parent_permissions(self):
        parent = self.root / ".config" / "gpt2codex"
        parent.mkdir(parents=True, mode=0o755)
        parent.chmod(0o755)
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            path = module.default_config_path()
            self.assertEqual(path, parent / "github-queue" / "config.json")
            module.init_config(path, "Pitriss/gpt2codex-queue")
            cfg = module.load_config(path)
        self.assertEqual(cfg["repository"], "Pitriss/gpt2codex-queue")
        self.assertEqual(parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_form_contract_and_deterministic_digest(self):
        issue = make_issue()
        a = module.parse_issue(issue, self.cfg)
        b = module.parse_issue(issue, self.cfg)
        self.assertIsNotNone(a)
        self.assertEqual(a["digest"], b["digest"])
        self.assertEqual(a["project"], "kencoder")
        self.assertEqual(a["mode"], "read-only")
        accented = issue.copy()
        accented["body"] = issue["body"].replace("Rezim", "Re\u017eim").replace("Zadani", "Zad\u00e1n\u00ed")
        self.assertIsNotNone(module.parse_issue(accented, self.cfg))

    def test_untrusted_author_and_bad_mode_ignored(self):
        self.assertIsNone(module.parse_issue(make_issue(login="random"), self.cfg))
        self.assertIsNone(module.parse_issue(make_issue(mode="danger-full-access"), self.cfg))
        self.assertIsNone(module.parse_issue(make_issue(project="../kencoder"), self.cfg))
        self.assertIsNone(module.parse_issue(make_issue(task="No response"), self.cfg))
        self.assertIsNone(module.parse_issue({**make_issue(), "title": "plain request"}, self.cfg))
        self.assertIsNone(module.parse_issue({**make_issue(), "state": "closed"}, self.cfg))

    def test_poll_does_not_approve_and_mutation_blocks_execution(self):
        gh = FakeGitHub([make_issue()])
        self.assertEqual(module.scan(gh, self.store, self.cfg), ["1"])
        self.assertEqual(module.scan(gh, self.store, self.cfg), [])
        with self.store.lock():
            state = self.store.load()
            self.assertEqual(state["jobs"]["1"]["status"], "pending")
        gh.values[1]["body"] = gh.values[1]["body"].replace("List files", "Delete everything")
        with self.assertRaises(module.QueueError):
            module.validated_remote(gh, self.store, self.cfg, 1, ("pending",))
        module.scan(gh, self.store, self.cfg)
        with self.store.lock():
            self.assertEqual(self.store.load()["jobs"]["1"]["status"], "pending")

    def test_approved_edit_becomes_stale(self):
        gh = FakeGitHub([make_issue()])
        module.scan(gh, self.store, self.cfg)
        with self.store.lock():
            state = self.store.load()
            state["jobs"]["1"]["status"] = "approved"
            self.store.save(state)
        gh.values[1]["body"] = gh.values[1]["body"].replace("List files", "Other task")
        module.scan(gh, self.store, self.cfg)
        with self.store.lock():
            self.assertEqual(self.store.load()["jobs"]["1"]["status"], "stale")
        with self.assertRaises(module.QueueError):
            module.run_one(gh, self.store, self.cfg, 1)

    def test_run_requires_approval_and_reports_metadata_only(self):
        gh = FakeGitHub([make_issue(mode="read-only")])
        module.scan(gh, self.store, self.cfg)
        with self.assertRaises(module.QueueError):
            module.run_one(gh, self.store, self.cfg, 1)
        with self.store.lock():
            state = self.store.load()
            state["jobs"]["1"]["status"] = "approved"
            self.store.save(state)
        self.assertEqual(module.run_one(gh, self.store, self.cfg, 1), 0)
        with self.store.lock():
            state = self.store.load()
            self.assertEqual(state["jobs"]["1"]["status"], "done")
            self.assertEqual(state["jobs"]["1"]["exit_code"], 0)
        self.assertEqual(gh.closed, [1])
        self.assertEqual(len(gh.comments), 1)
        self.assertNotIn("List files", gh.comments[0][1])
        self.assertNotIn("MOCK RESULT", gh.comments[0][1])
        self.assertEqual((self.store.log_dir / "issue-1.stdout.log").read_text(), "MOCK RESULT\n")
        self.assertEqual((self.store.log_dir / "issue-1.stdout.log").stat().st_mode & 0o077, 0)
        with self.assertRaises(module.QueueError):
            module.run_one(gh, self.store, self.cfg, 1)

    def test_reject_does_not_run(self):
        gh = FakeGitHub([make_issue()])
        module.scan(gh, self.store, self.cfg)
        module.reject(self.store, 1)
        module.run_approved(gh, self.store, self.cfg)
        self.assertFalse(gh.comments)
        self.assertFalse(gh.closed)

    def test_noninteractive_approval_never_consumes_input_or_launches(self):
        gh = FakeGitHub([make_issue()])
        module.scan(gh, self.store, self.cfg)
        digest = module.parse_issue(gh.issue(1), self.cfg)["digest"]
        with mock.patch.object(module.sys.stdin, "isatty", return_value=False), \
                mock.patch("builtins.input") as answer, \
                mock.patch.object(module.subprocess, "Popen") as launch:
            with self.assertRaises(module.QueueError):
                module.approve(gh, self.store, self.cfg, 1, digest)
            module.run_approved(gh, self.store, self.cfg)
        answer.assert_not_called()
        launch.assert_not_called()
        with self.store.lock():
            self.assertEqual(self.store.load()["jobs"]["1"]["status"], "pending")
        self.assertFalse(gh.comments)

    def test_edit_during_interactive_approval_is_rejected(self):
        gh = FakeGitHub([make_issue()])
        module.scan(gh, self.store, self.cfg)
        digest = module.parse_issue(gh.issue(1), self.cfg)["digest"]

        def edit_then_confirm(_prompt):
            gh.values[1]["body"] += "Changed after display\n"
            return "SCHVALUJI 1"

        with mock.patch.object(module.sys.stdin, "isatty", return_value=True), \
                mock.patch("builtins.input", side_effect=edit_then_confirm):
            with self.assertRaises(module.QueueError):
                module.approve(gh, self.store, self.cfg, 1, digest)
        with self.store.lock():
            self.assertEqual(self.store.load()["jobs"]["1"]["status"], "pending")

    def test_approved_sha_change_blocks_before_claim_without_rescan(self):
        gh = FakeGitHub([make_issue()])
        module.scan(gh, self.store, self.cfg)
        with self.store.lock():
            state = self.store.load()
            state["jobs"]["1"]["status"] = "approved"
            self.store.save(state)
        gh.values[1]["body"] += "Edited before execution\n"
        with mock.patch.object(module.subprocess, "Popen") as launch:
            with self.assertRaises(module.QueueError):
                module.run_one(gh, self.store, self.cfg, 1)
        launch.assert_not_called()
        self.assertFalse(gh.comments)
        self.assertEqual(list(self.store.log_dir.iterdir()), [])

    def test_duplicate_issue_cannot_replace_original_issue_id(self):
        gh = FakeGitHub([make_issue()])
        module.scan(gh, self.store, self.cfg)
        before = self.store.file.read_bytes()
        gh.values[1]["id"] += 1
        with self.assertRaises(module.QueueError):
            module.scan(gh, self.store, self.cfg)
        self.assertEqual(self.store.file.read_bytes(), before)

    def test_worker_crash_after_claim_is_not_replayed_after_restart(self):
        gh = FakeGitHub([make_issue()])
        module.scan(gh, self.store, self.cfg)
        with self.store.lock():
            state = self.store.load()
            state["jobs"]["1"]["status"] = "approved"
            self.store.save(state)

        def crash_after_durable_claim(*_args, **_kwargs):
            with self.store.lock():
                self.assertEqual(self.store.load()["jobs"]["1"]["status"], "running")
            raise KeyboardInterrupt("synthetic worker crash; no process started")

        with mock.patch.object(module.subprocess, "Popen", side_effect=crash_after_durable_claim) as launch:
            with self.assertRaises(KeyboardInterrupt):
                module.run_one(gh, self.store, self.cfg, 1)
            launch.assert_called_once()
        restarted = module.Store(self.root)
        with mock.patch.object(module.subprocess, "Popen") as launch:
            self.assertEqual(module.scan(gh, restarted, self.cfg), [])
            module.run_approved(gh, restarted, self.cfg)
            with self.assertRaises(module.QueueError):
                module.run_one(gh, restarted, self.cfg, 1)
        launch.assert_not_called()
        with restarted.lock():
            self.assertEqual(restarted.load()["jobs"]["1"]["status"], "running")
        self.assertFalse(gh.comments)
        self.assertFalse(gh.closed)


    def test_summary_must_live_in_private_tmp_area(self):
        gh = FakeGitHub([make_issue()])
        module.scan(gh, self.store, self.cfg)
        with self.store.lock():
            state = self.store.load()
            state["jobs"]["1"]["status"] = "done"
            self.store.save(state)
        outside = self.root / "private.md"
        outside.write_text("approved text")
        outside.chmod(0o600)
        with mock.patch.object(module.sys.stdin, "isatty", return_value=True):
            with self.assertRaises(module.QueueError):
                module.publish_summary(gh, self.store, self.cfg, 1, outside)
        self.assertFalse(gh.comments)

    def test_missing_token_permissions(self):
        path = self.root / "token"
        path.write_text("github_pat_FAKE123")
        path.chmod(0o644)
        with self.assertRaises(module.QueueError):
            module.secure_private_file(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
