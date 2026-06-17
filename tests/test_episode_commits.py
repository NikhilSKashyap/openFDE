"""
Tests for openfde.episode_commits — prompt → commit reconciliation (many prompts, one commit).

Laws under test:
  - A commit declares episodes via OpenFDE-Episodes (plural) and/or OpenFDE-Episode (singular).
  - An explicit trailer ALWAYS wins, with no other gate.
  - Inference is provenance-gated: a trailer-less commit only attaches to a SAME-repo episode that
    shares files AND provably belongs to its turn — inside the capture window (createdAt, never
    updatedAt), OR baseline-matched (first parent == initialHead), OR the latest open/reviewing
    work unit with fresh activity. Strong file overlap is NOT a bypass.
  - needs_manual_land is not "active forever": an old one is stale historical (window/baseline only).
  - Confidence: explicit > high_file_overlap (multi-file, strong) > time_file_inferred > ambiguous.
  - A new commit must NOT attach to stale historical episodes (incl. old needs_manual_land) that
    merely share files.
"""
import unittest
from datetime import datetime, timedelta, timezone

from openfde import episode_commits as ec


def _iso(dt):
    return dt.isoformat()


NOW = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=3)
H12 = NOW - timedelta(hours=12)         # outside the 6h capture window, inside the 24h active window
H13 = NOW - timedelta(hours=13)


def _ep(eid, files, *, created=NOW, status="reviewing", signal="product",
        session_cwd=None, operational=False, initial_head=None):
    """A candidate-shaped episode. Defaults are 'attachable' (recent, active, product) so each
    test overrides exactly the one field it is exercising."""
    e = {"episodeId": eid, "files": list(files), "createdAt": _iso(created),
         "status": status, "signal": signal}
    if session_cwd is not None:
        e["sessionCwd"] = session_cwd
    if initial_head is not None:
        e["initialHead"] = initial_head
    if operational:
        e["signal"] = "operational"
        e["storyFacts"] = {"operational": True}
    return e


def _commit(sha, files, *, ids=None, ts=NOW, parents=None,
            author="OpenFDE", email="openfde@localhost"):
    return {"sha": sha, "files": list(files), "episodeIds": list(ids or []),
            "timestamp": _iso(ts), "parents": list(parents or []),
            "author": author, "email": email}


class TrailerParseTest(unittest.TestCase):
    def test_plural_comma_separated(self):
        self.assertEqual(ec.episode_ids_from_trailers({"OpenFDE-Episodes": "e1, e2, e3"}),
                         ["e1", "e2", "e3"])

    def test_plural_space_separated(self):
        self.assertEqual(ec.episode_ids_from_trailers({"OpenFDE-Episodes": "e1 e2"}), ["e1", "e2"])

    def test_singular_and_plural_dedup_order(self):
        ids = ec.episode_ids_from_trailers({"OpenFDE-Episodes": "e1,e2", "OpenFDE-Episode": "e1"})
        self.assertEqual(ids, ["e1", "e2"])

    def test_singular_only(self):
        self.assertEqual(ec.episode_ids_from_trailers({"OpenFDE-Episode": "solo"}), ["solo"])

    def test_none_when_no_trailers(self):
        self.assertEqual(ec.episode_ids_from_trailers({}), [])


class ReconcileCommitTest(unittest.TestCase):
    def test_explicit_trailer_wins_even_without_file_overlap_or_recency(self):
        ep = {"episodeId": "P9", "files": [], "createdAt": _iso(OLD), "status": "landed"}
        [v] = ec.reconcile_commit(_commit("abc", ["x.py"], ids=["P9"]), [ep])
        self.assertEqual((v["confidence"], v["attach"]), ("explicit", True))

    def test_multi_file_strong_overlap_recent_is_high(self):
        ep = _ep("P1", ["a.py", "b.py"])
        [v] = ec.reconcile_commit(_commit("c1", ["a.py", "b.py", "README.md"]), [ep])
        self.assertEqual(v["confidence"], "high_file_overlap")
        self.assertEqual(v["matchedFiles"], ["a.py", "b.py"])
        self.assertTrue(v["attach"])

    def test_weak_multi_overlap_recent_is_time_inferred(self):
        ep = _ep("P2", ["a.py", "b.py", "c.py", "d.py"])
        [v] = ec.reconcile_commit(_commit("c2", ["a.py"]), [ep])
        self.assertEqual(v["confidence"], "time_file_inferred")
        self.assertTrue(v["attach"])

    def test_one_file_near_attaches_as_time_inferred(self):
        [v] = ec.reconcile_commit(_commit("c", ["a.py"]), [_ep("P", ["a.py"])])
        self.assertEqual(v["confidence"], "time_file_inferred")
        self.assertTrue(v["attach"])

    def test_one_file_far_in_time_not_attached(self):
        ep = _ep("P", ["a.py"], created=OLD, status="open")
        [v] = ec.reconcile_commit(_commit("c", ["a.py"]), [ep])
        self.assertFalse(v["attach"])

    def test_uses_createdAt_not_polluted_updatedAt(self):
        # An ancient episode re-summarized "now" (updatedAt bumped) must NOT look active.
        ep = _ep("P", ["a.py"], created=OLD)
        ep["updatedAt"] = _iso(NOW)
        [v] = ec.reconcile_commit(_commit("c", ["a.py"]), [ep])
        self.assertFalse(v["attach"])

    def test_old_needs_manual_land_strong_overlap_not_attached(self):
        # THE P50/P52 bug: an old needs_manual_land with strong multi-file overlap is stale
        # historical — strong overlap is NOT a timing bypass, and needs_manual_land isn't "active".
        ep = _ep("P52", ["a.py", "b.py", "c.py"], created=OLD, status="needs_manual_land")
        [v] = ec.reconcile_commit(_commit("new", ["a.py", "b.py", "c.py"]), [ep])
        self.assertEqual(v["confidence"], "ambiguous")
        self.assertFalse(v["attach"])

    def test_baseline_match_attaches_despite_old_capture(self):
        # A long multi-day session: createdAt is old, but the commit's first parent is the episode's
        # captured baseline (initialHead) — proof it IS this turn's work, independent of timing.
        ep = _ep("P", ["a.py", "b.py"], created=OLD, status="needs_manual_land", initial_head="BASE")
        [v] = ec.reconcile_commit(_commit("c", ["a.py", "b.py"], parents=["BASE"]), [ep])
        self.assertEqual(v["confidence"], "high_file_overlap")
        self.assertTrue(v["attach"])
        self.assertIn("baseline", v["reason"])

    def test_latest_active_gets_wider_window_only_for_itself(self):
        # The single latest open/reviewing episode gets the wider active window (12h here); an
        # older active episode just outside the capture window does not.
        latest = _ep("latest", ["a.py"], created=H12, status="reviewing")
        older = _ep("older", ["a.py"], created=H13, status="reviewing")
        out = {v["episodeId"]: v for v in ec.reconcile_commit(_commit("c", ["a.py"]), [latest, older])}
        self.assertTrue(out["latest"]["attach"])
        self.assertFalse(out["older"]["attach"])

    def test_different_repo_is_excluded(self):
        commit = _commit("c", ["frontend/src/App.jsx"])
        same = _ep("same", ["frontend/src/App.jsx"], session_cwd="/nonexistent/watched")
        other = _ep("other", ["frontend/src/App.jsx"], session_cwd="/nonexistent/sibling")
        out = ec.reconcile_commit(commit, [same, other], watched_root="/nonexistent/watched")
        self.assertEqual([v["episodeId"] for v in out if v["attach"]], ["same"])

    def test_operational_episode_needs_strong_evidence(self):
        commit = _commit("c", ["a.py", "b.py"])
        weak = _ep("op1", ["a.py"], operational=True)
        strong = _ep("op2", ["a.py", "b.py"], operational=True)
        out = {v["episodeId"]: v for v in ec.reconcile_commit(commit, [weak, strong])}
        self.assertFalse(out["op1"]["attach"])
        self.assertTrue(out["op2"]["attach"])

    def test_zero_file_episode_never_attached_without_trailer(self):
        self.assertEqual(ec.reconcile_commit(_commit("c", ["a.py"]), [_ep("d", [])]), [])

    def test_no_shared_file_no_link(self):
        self.assertEqual(ec.reconcile_commit(_commit("c", ["a.py"]), [_ep("P", ["z.py"])]), [])

    def test_baseline_match_bypasses_the_repo_gate(self):
        # cwd-agnostic capture (P119): the episode's sessionCwd is a DIFFERENT repo, but the commit
        # baseline-matches (first parent == initialHead, a watched-repo sha) → still attached.
        ep = _ep("P119", ["tests/test_plugins.py"], status="complete_no_changes",
                 session_cwd="/some/other/repo", initial_head="BASE")
        commit = _commit("c", ["openfde/plugins.py", "tests/test_plugins.py"], parents=["BASE"])
        [v] = ec.reconcile_commit(commit, [ep], watched_root="/the/watched/repo")
        self.assertTrue(v["attach"])
        self.assertIn("baseline", v["reason"])


class ReconcileAuthoredEpisodesTest(unittest.TestCase):
    """Conservative rail attribution: trailer wins; the heuristic attaches OpenFDE-authored commits
    on a single unambiguous match and marks the episode landed; ambiguous overlap is refused."""

    def test_explicit_trailer_wins_and_marks_landed(self):
        ep = _ep("P9", ["x.py"], status="reviewing")
        # foreign-authored, no file overlap — still attaches via its trailer (authoritative).
        commit = _commit("abc", ["unrelated.py"], ids=["P9"], author="A Human", email="dev@x.com")
        changed = ec.reconcile_authored_episodes([commit], [ep])
        self.assertIn("P9", changed)
        self.assertEqual(ep["commitShas"], ["abc"])
        self.assertEqual(ep["status"], "landed")

    def test_explicit_trailer_attaches_to_complete_no_changes_outside_autoland(self):
        # The P155 gap: a trailer'd commit made OUTSIDE autoland (a manual / external land — here a
        # human author) onto an episode ALREADY classified complete_no_changes (its files were
        # committed, so not dirty) with no recorded commit. The trailer is authoritative, so it must
        # still attach + flip the episode to landed. (The server's candidate filter must likewise not
        # pre-exclude trailer-carrying commits, or such a commit never reaches this path.)
        ep = _ep("P155", ["openfde/focus.py", "frontend/src/components/Focus/FocusLens.jsx"],
                 status="complete_no_changes")
        commit = _commit("9720d9d", ["openfde/focus.py"], ids=["P155"],
                         author="NikhilSKashyap", email="dev@example.com")
        changed = ec.reconcile_authored_episodes([commit], [ep])
        self.assertIn("P155", changed)
        self.assertEqual(ep["commitShas"], ["9720d9d"])
        self.assertEqual(ep["status"], "landed")

    def test_heuristic_attaches_strong_overlap_and_marks_landed(self):
        ep = _ep("P1", ["a.py", "b.py"], status="reviewing")
        changed = ec.reconcile_authored_episodes([_commit("c1", ["a.py", "b.py", "extra.py"])], [ep])
        self.assertIn("P1", changed)
        self.assertEqual(ep["commitShas"], ["c1"])
        self.assertEqual(ep["commitMeta"]["c1"]["confidence"], "high_file_overlap")
        self.assertEqual(ep["status"], "landed")

    def test_heuristic_attaches_across_cwd_mismatch_via_baseline(self):
        # The P119 gap end-to-end: different sessionCwd + complete_no_changes, but baseline-matched.
        ep = _ep("P119", ["tests/test_plugins.py"], status="complete_no_changes",
                 session_cwd="/some/other/repo", initial_head="BASE")
        commit = _commit("4975551", ["openfde/plugins.py", "tests/test_plugins.py"], parents=["BASE"])
        changed = ec.reconcile_authored_episodes([commit], [ep], watched_root="/the/watched/repo")
        self.assertIn("P119", changed)
        self.assertEqual(ep["commitShas"], ["4975551"])
        self.assertEqual(ep["status"], "landed")

    def test_heuristic_refuses_ambiguous_multi_episode_overlap(self):
        # Two episodes both claim the SAME file → can't tell which prompt owns it → attach to NEITHER.
        a, b = _ep("A", ["shared.py"], status="reviewing"), _ep("B", ["shared.py"], status="reviewing")
        changed = ec.reconcile_authored_episodes([_commit("c", ["shared.py"])], [a, b])
        self.assertEqual(changed, {})
        self.assertNotIn("commitShas", a)
        self.assertNotIn("commitShas", b)
        self.assertEqual(a["status"], "reviewing")

    def test_disjoint_overlap_still_multi_attaches(self):
        # DISTINCT files per episode → many prompts → one commit still works (not ambiguous).
        a, b = _ep("A", ["a.py"], status="reviewing"), _ep("B", ["b.py"], status="reviewing")
        changed = ec.reconcile_authored_episodes([_commit("c", ["a.py", "b.py"])], [a, b])
        self.assertEqual(set(changed), {"A", "B"})

    def test_foreign_authored_commit_is_not_heuristically_attached(self):
        ep = _ep("P1", ["a.py", "b.py"], status="reviewing")
        commit = _commit("c", ["a.py", "b.py"], author="A Human", email="dev@human.com")
        self.assertEqual(ec.reconcile_authored_episodes([commit], [ep]), {})
        self.assertNotIn("commitShas", ep)
        self.assertEqual(ep["status"], "reviewing")           # not flipped to landed

    # ── multi-commit: one prompt owns every commit it produced ───────────────
    def test_two_sequential_trailerless_commits_both_attach(self):
        # c1 lands on the episode's baseline; c2 chains off c1 — both belong to the one prompt.
        ep = _ep("P1", ["a.py", "b.py"], status="reviewing", initial_head="BASE")
        c1 = _commit("c1", ["a.py", "b.py"], parents=["BASE"], ts=NOW - timedelta(minutes=5))
        c2 = _commit("c2", ["a.py", "b.py", "c.py"], parents=["c1"], ts=NOW)
        ec.reconcile_authored_episodes([c2, c1], [ep])            # newest-first input
        self.assertEqual(ep["commitShas"], ["c1", "c2"])         # processed oldest-first → both land
        self.assertEqual(ep["status"], "landed")
        self.assertIn("chains", ep["commitMeta"]["c2"]["reason"].lower())

    def test_second_commit_chains_across_cwd_mismatch(self):
        # The P121 gap: sessionCwd is a DIFFERENT repo, c1 already attached, c2 chains off c1 — so
        # the repo gate is bypassed (chain is repo-specific proof) and c2 lands on the same prompt.
        ep = _ep("P121", ["a.py", "b.py"], status="landed",
                 session_cwd="/some/other/repo", initial_head="BASE")
        ep["commitShas"] = ["c1"]
        c2 = _commit("c2", ["a.py", "b.py"], parents=["c1"])
        changed = ec.reconcile_authored_episodes([c2], [ep], watched_root="/the/watched/repo")
        self.assertIn("P121", changed)
        self.assertEqual(ep["commitShas"], ["c1", "c2"])

    def test_ambiguous_second_commit_refuses(self):
        # A new commit chains off NEITHER landed episode and shares the SAME file with both → refuse.
        a = _ep("A", ["shared.py"], status="landed")
        b = _ep("B", ["shared.py"], status="landed")
        a["commitShas"], b["commitShas"] = ["ca"], ["cb"]
        c2 = _commit("c2", ["shared.py"], parents=["zzz"])       # chains off neither
        self.assertEqual(ec.reconcile_authored_episodes([c2], [a, b]), {})
        self.assertEqual(a["commitShas"], ["ca"])
        self.assertEqual(b["commitShas"], ["cb"])

    def test_explicit_trailer_wins_over_chain_heuristic(self):
        # c2 chains off P1's commit (heuristic would point at P1) but its trailer names P2 → P2 wins.
        p1 = _ep("P1", ["a.py"], status="landed")
        p1["commitShas"] = ["c1"]
        p2 = _ep("P2", ["z.py"], status="reviewing")
        c2 = _commit("c2", ["a.py"], ids=["P2"], parents=["c1"])
        ec.reconcile_authored_episodes([c2], [p1, p2])
        self.assertIn("c2", p2["commitShas"])                    # explicit trailer wins
        self.assertNotIn("c2", (p1.get("commitShas") or []))     # heuristic chain is overridden


class AttachCommitTest(unittest.TestCase):
    def test_idempotent_no_duplicate_sha(self):
        ep = {"episodeId": "P1"}
        ec.attach_commit(ep, "sha1", confidence="high_file_overlap", matched_files=["a.py"])
        ec.attach_commit(ep, "sha1", confidence="high_file_overlap", matched_files=["a.py"])
        self.assertEqual(ep["commitShas"], ["sha1"])
        self.assertEqual(ep["commitMeta"]["sha1"]["confidence"], "high_file_overlap")

    def test_stronger_confidence_upgrades(self):
        ep = {"episodeId": "P1"}
        ec.attach_commit(ep, "sha1", confidence="time_file_inferred", matched_files=["a.py"])
        ec.attach_commit(ep, "sha1", confidence="explicit", matched_files=["a.py"])
        self.assertEqual(ep["commitMeta"]["sha1"]["confidence"], "explicit")

    def test_weaker_confidence_does_not_downgrade(self):
        ep = {"episodeId": "P1"}
        ec.attach_commit(ep, "sha1", confidence="explicit", matched_files=["a.py"])
        ec.attach_commit(ep, "sha1", confidence="ambiguous", matched_files=["a.py"])
        self.assertEqual(ep["commitMeta"]["sha1"]["confidence"], "explicit")


class ReconcileEpisodesTest(unittest.TestCase):
    def test_one_commit_lands_on_three_fresh_prompt_cards(self):
        eps = [_ep("P1", ["a.py"]), _ep("P2", ["b.py"]), _ep("P3", ["c.py"])]
        commit = _commit("batched", ["a.py", "b.py", "c.py"])
        changed = ec.reconcile_episodes([commit], eps)
        self.assertEqual(set(changed), {"P1", "P2", "P3"})
        for ep in eps:
            self.assertEqual(ep["commitShas"], ["batched"])
            self.assertEqual(ep["commitMeta"]["batched"]["confidence"], "time_file_inferred")

    def test_ambiguous_excluded_by_default_included_on_request(self):
        ep = _ep("P3", ["a.py", "b.py", "c.py", "d.py"], created=OLD, status="open")
        commit = _commit("c3", ["a.py"])
        ec.reconcile_episodes([commit], [ep])
        self.assertNotIn("commitShas", ep)
        ec.reconcile_episodes([commit], [ep], include_ambiguous=True)
        self.assertEqual(ep["commitShas"], ["c3"])
        self.assertEqual(ep["commitMeta"]["c3"]["confidence"], "ambiguous")


class PrecisionRegressionTest(unittest.TestCase):
    """Regression for over-attachment: a new commit must attach to ZERO stale historical episodes,
    including old needs_manual_land with strong multi-file overlap (the real P50/P52 probe)."""

    def test_new_commit_does_not_attach_to_stale_history(self):
        WATCHED, SIBLING = "/nonexistent/watched", "/nonexistent/sibling"
        stale = []
        # 14 cross-repo episodes (sibling repo) touching the same RELATIVE path — coincidence.
        stale += [_ep(f"x{i}", ["README.md"], created=NOW, status="reviewing", session_cwd=SIBLING)
                  for i in range(14)]
        # 13 same-repo historical (landed, days old) 1-file episodes on the common file.
        stale += [_ep(f"o{i}", ["README.md"], created=OLD, status="landed", session_cwd=WATCHED)
                  for i in range(13)]
        # P50/P52-shaped: old needs_manual_land with STRONG multi-file overlap to the commit.
        stale += [_ep(f"p{i}", ["openfde/prs.py", "openfde/server.py"], created=OLD,
                      status="needs_manual_land", session_cwd=WATCHED) for i in range(8)]
        # One genuinely fresh, in-repo, active episode whose files the commit actually changed.
        fresh = _ep("fresh", ["openfde/cli.py"], created=NOW, status="reviewing", session_cwd=WATCHED)
        episodes = stale + [fresh]

        commit = _commit("new", ["README.md", "openfde/prs.py", "openfde/server.py", "openfde/cli.py"])
        changed = ec.reconcile_episodes([commit], episodes, watched_root=WATCHED)

        self.assertEqual(set(changed), {"fresh"})                       # ONLY the fresh episode
        self.assertEqual(sum(1 for e in stale if e.get("commitShas")), 0)  # zero stale attachments
        self.assertEqual(fresh["commitShas"], ["new"])


if __name__ == "__main__":
    unittest.main()
