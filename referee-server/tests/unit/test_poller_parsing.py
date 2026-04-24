"""Unit tests for the ``Poller`` shell-output parsing and normalisation helpers.

Nothing here actually talks over SSH: every test either calls static helpers
on ``Poller`` or drives it with the recording ``DummySSH`` double from
``conftest.py``.
"""
from __future__ import annotations

import unittest
from datetime import UTC, datetime

import pytest

from poller import MISSING_HASH, Poller, VariantSnapshot

from tests.conftest import DummySSH


pytestmark = pytest.mark.unit


class PollerCompletenessTests(unittest.TestCase):
    def test_partial_output_synthesizes_missing_variants_as_failed(self) -> None:
        from config import SETTINGS  # imported lazily so SETTINGS reflects autouse fixture

        class PartialSSH:
            def exec(self, host: str, command: str):
                _ = command
                return (
                    1,
                    "\n".join(
                        [
                            "===VARIANT:A===",
                            "===KING===",
                            "Team Alpha",
                            "===KING_STAT===",
                            "1000 644 root:root regular file",
                            "===END_VARIANT===",
                        ]
                    ),
                    "simulated failure",
                )

        poller = Poller(PartialSSH())

        snapshots, violations = poller.run_cycle(series=1)

        self.assertEqual(len(snapshots), len(SETTINGS.node_hosts) * len(SETTINGS.variants))
        self.assertEqual({snap.variant for snap in snapshots}, {"A", "B", "C"})
        self.assertEqual({snap.node_host for snap in snapshots}, set(SETTINGS.node_hosts))
        self.assertEqual(
            {snap.variant for snap in snapshots if snap.status == "failed"},
            {"B", "C"},
        )
        self.assertEqual(violations, {})

    def test_watchdog_detection_does_not_flag_one_shot_king_write_command(self) -> None:
        poller = Poller(DummySSH())
        snap = VariantSnapshot(
            node_host="192.168.0.102",
            variant="C",
            king="Team Alpha",
            king_mtime_epoch=1000,
            status="running",
            sections={
                "KING_STAT": "1000 644 root:root regular file",
                "KING": "Team Alpha",
                "ROOT_DIR": "700",
                "IMMUTABLE": "",
                "CRON": "",
                "PROCS": "root  42  0.0  bash -p -c 'echo Team Alpha > /root/king.txt'",
            },
            checked_at=datetime.now(UTC),
        )

        hits = poller._detect_violations(snap)

        self.assertFalse(any(hit.offense_name == "watchdog_process" for hit in hits))

    def test_probe_command_uses_root_exec_and_separates_king_section(self) -> None:
        poller = Poller(DummySSH())

        command = poller._build_probe_command(series=2)

        self.assertIn('docker exec -u 0 "$container_id" sh -lc', command)
        self.assertIn('printf "\\n";', command)

    def test_normalize_king_strips_inline_section_marker(self) -> None:
        self.assertEqual(Poller._normalize_king("unclaimed===KING_STAT==="), "unclaimed")

    def test_stable_ports_signature_ignores_docker_dns_stub_port(self) -> None:
        poller = Poller(DummySSH())
        first = """State  Recv-Q Send-Q Local Address:Port Peer Address:Port
LISTEN 0 4096 127.0.0.11:38271 0.0.0.0:*
LISTEN 0 1 [::ffff:127.0.0.1]:8005 *:*
LISTEN 0 100 *:8080 *:*
"""
        second = """State  Recv-Q Send-Q Local Address:Port Peer Address:Port
LISTEN 0 4096 127.0.0.11:46209 0.0.0.0:*
LISTEN 0 100 *:8080 *:*
LISTEN 0 1 [::ffff:127.0.0.1]:8005 *:*
"""
        changed = """State  Recv-Q Send-Q Local Address:Port Peer Address:Port
LISTEN 0 4096 127.0.0.11:46209 0.0.0.0:*
LISTEN 0 100 *:8081 *:*
LISTEN 0 1 [::ffff:127.0.0.1]:8005 *:*
"""
        self.assertEqual(
            poller.stable_ports_signature(first),
            poller.stable_ports_signature(second),
        )
        self.assertNotEqual(
            poller.stable_ports_signature(first),
            poller.stable_ports_signature(changed),
        )


# ---------------------------------------------------------------------------
# Edge cases for is_valid_team_claim.
#
# The filter sits at the scorer boundary and has to reject any byte sequence
# that would later trip over a shell here-doc, a SQL parameter binding, or a
# terminal control sequence. Boundary conditions (length, control-character
# range, case folding for ``unclaimed``) all live here.
# ---------------------------------------------------------------------------
class IsValidTeamClaimEdgeCases(unittest.TestCase):
    def test_rejects_empty_string(self) -> None:
        self.assertFalse(Poller.is_valid_team_claim(""))

    def test_rejects_whitespace_only(self) -> None:
        self.assertFalse(Poller.is_valid_team_claim("   \t  "))

    def test_accepts_single_character(self) -> None:
        self.assertTrue(Poller.is_valid_team_claim("A"))

    def test_accepts_exactly_128_chars(self) -> None:
        # 128 is the documented maximum inclusive boundary.
        self.assertTrue(Poller.is_valid_team_claim("A" * 128))

    def test_rejects_129_chars(self) -> None:
        self.assertFalse(Poller.is_valid_team_claim("A" * 129))

    def test_rejects_exact_unclaimed_case_insensitive(self) -> None:
        for variant in ("unclaimed", "UNCLAIMED", "Unclaimed", "UnClaImEd"):
            with self.subTest(variant=variant):
                self.assertFalse(Poller.is_valid_team_claim(variant))

    def test_accepts_suffix_unclaimedd(self) -> None:
        # ``unclaimedd`` is not the reserved sentinel — must be accepted
        # so operators can legitimately create a team with that name.
        self.assertTrue(Poller.is_valid_team_claim("unclaimedd"))

    def test_accepts_team_name_with_embedded_spaces(self) -> None:
        self.assertTrue(Poller.is_valid_team_claim("Team Alpha Squadron"))

    def test_rejects_newline_in_middle(self) -> None:
        # A claim that survived into king.txt with an embedded newline
        # would corrupt the multi-line sections parser on the next poll.
        self.assertFalse(Poller.is_valid_team_claim("Team\nAlpha"))

    def test_rejects_carriage_return(self) -> None:
        self.assertFalse(Poller.is_valid_team_claim("Team\rAlpha"))

    def test_rejects_tab(self) -> None:
        self.assertFalse(Poller.is_valid_team_claim("Team\tAlpha"))

    def test_rejects_null_byte(self) -> None:
        self.assertFalse(Poller.is_valid_team_claim("Team\x00Alpha"))

    def test_rejects_ansi_escape(self) -> None:
        self.assertFalse(Poller.is_valid_team_claim("Team\x1bAlpha"))

    def test_accepts_unicode_nonbreaking_space(self) -> None:
        # U+00A0 is >= 32, so it passes the control-character filter. This
        # is intentional — many team names use NBSPs for layout.
        self.assertTrue(Poller.is_valid_team_claim("Team Alpha"))

    def test_accepts_unicode_emoji(self) -> None:
        self.assertTrue(Poller.is_valid_team_claim("Team 🚀"))

    def test_accepts_boundary_character_space(self) -> None:
        # chr(32) is the lowest accepted value.
        self.assertTrue(Poller.is_valid_team_claim("A B"))


# ---------------------------------------------------------------------------
# Edge cases for _normalize_king.
# ---------------------------------------------------------------------------
class NormalizeKingEdgeCases(unittest.TestCase):
    def test_empty_string_returns_none(self) -> None:
        self.assertIsNone(Poller._normalize_king(""))

    def test_only_whitespace_returns_none(self) -> None:
        # Whitespace-only first line is still "nothing to see here."
        self.assertIsNone(Poller._normalize_king("   \n\n"))

    def test_only_file_missing_returns_none(self) -> None:
        self.assertIsNone(Poller._normalize_king("FILE_MISSING"))

    def test_file_missing_with_trailing_content_returns_none(self) -> None:
        # _normalize_king takes only the first line. If that line is
        # FILE_MISSING, the file is missing — period.
        self.assertIsNone(Poller._normalize_king("FILE_MISSING\nextra garbage"))

    def test_multiline_returns_first_line_stripped(self) -> None:
        self.assertEqual(Poller._normalize_king("Team Alpha\ngarbage\nafter"), "Team Alpha")

    def test_strips_inline_section_marker(self) -> None:
        self.assertEqual(Poller._normalize_king("Team===KING_STAT==="), "Team")

    def test_two_inline_markers_takes_first_fragment(self) -> None:
        self.assertEqual(Poller._normalize_king("Foo===Bar===Baz"), "Foo")

    def test_only_inline_marker_returns_none(self) -> None:
        # Everything before the first ``===`` is whitespace, so after
        # stripping nothing is left — return None rather than an empty
        # string so downstream code can distinguish "no claim" from "empty
        # claim".
        self.assertIsNone(Poller._normalize_king("===KING_STAT==="))

    def test_leading_and_trailing_whitespace_stripped(self) -> None:
        self.assertEqual(Poller._normalize_king("  Team Alpha  "), "Team Alpha")


# ---------------------------------------------------------------------------
# Edge cases for _parse_mtime.
# ---------------------------------------------------------------------------
class ParseMtimeEdgeCases(unittest.TestCase):
    def test_empty_string_returns_none(self) -> None:
        self.assertIsNone(Poller._parse_mtime(""))

    def test_stat_fail_returns_none(self) -> None:
        self.assertIsNone(Poller._parse_mtime("STAT_FAIL"))

    def test_stat_fail_anywhere_in_output_returns_none(self) -> None:
        # ``STAT_FAIL`` anywhere in the blob flags the probe as failed, even
        # if there is numeric-looking text on the first line.
        self.assertIsNone(Poller._parse_mtime("1234 644 root:root STAT_FAIL"))

    def test_non_integer_first_token_returns_none(self) -> None:
        self.assertIsNone(Poller._parse_mtime("abc 644 root:root regular file"))

    def test_valid_epoch_returns_int(self) -> None:
        self.assertEqual(Poller._parse_mtime("1234 644 root:root regular file"), 1234)

    def test_first_line_only(self) -> None:
        self.assertEqual(Poller._parse_mtime("1234\n5678 garbage"), 1234)

    def test_leading_whitespace_on_first_line(self) -> None:
        self.assertEqual(Poller._parse_mtime("   1234 644 root:root regular file"), 1234)

    def test_only_newlines_returns_none(self) -> None:
        self.assertIsNone(Poller._parse_mtime("\n\n\n"))

    def test_epoch_zero_is_accepted(self) -> None:
        # Epoch 0 is January 1 1970 — a legitimate, if rarely useful,
        # value. The parser must return 0 rather than treating it as
        # "missing."
        self.assertEqual(Poller._parse_mtime("0 644 root:root regular file"), 0)


# ---------------------------------------------------------------------------
# Edge cases for _detect_violations.
# ---------------------------------------------------------------------------
def _violation_snap(**sections: str) -> VariantSnapshot:
    return VariantSnapshot(
        node_host="192.168.0.102",
        variant="A",
        king="Team Alpha",
        king_mtime_epoch=1000,
        status="running",
        sections=sections,
        checked_at=datetime.now(UTC),
    )


class DetectViolationsEdgeCases(unittest.TestCase):
    def test_empty_sections_yields_no_hits(self) -> None:
        hits = Poller(DummySSH())._detect_violations(_violation_snap())
        self.assertEqual(hits, [])

    def test_king_stat_fewer_than_four_fields_is_ignored(self) -> None:
        # ``_detect_violations`` requires at least 4 whitespace-separated
        # fields in KING_STAT before it inspects anything; truncated
        # output must be a no-op, not an IndexError.
        hits = Poller(DummySSH())._detect_violations(
            _violation_snap(KING_STAT="1000 644 root:root")  # 3 fields
        )
        names = {hit.offense_name for hit in hits}
        self.assertNotIn("king_perm_changed", names)
        self.assertNotIn("king_owner_changed", names)

    def test_king_perm_755_is_flagged(self) -> None:
        hits = Poller(DummySSH())._detect_violations(
            _violation_snap(KING_STAT="1000 755 root:root regular file")
        )
        self.assertTrue(any(hit.offense_name == "king_perm_changed" for hit in hits))

    def test_king_perm_644_is_not_flagged(self) -> None:
        hits = Poller(DummySSH())._detect_violations(
            _violation_snap(KING_STAT="1000 644 root:root regular file")
        )
        self.assertFalse(any(hit.offense_name == "king_perm_changed" for hit in hits))

    def test_king_owner_nonroot_is_flagged(self) -> None:
        hits = Poller(DummySSH())._detect_violations(
            _violation_snap(KING_STAT="1000 644 nobody:nogroup regular file")
        )
        self.assertTrue(any(hit.offense_name == "king_owner_changed" for hit in hits))

    def test_king_type_symbolic_link_is_flagged(self) -> None:
        hits = Poller(DummySSH())._detect_violations(
            _violation_snap(KING_STAT="1000 644 root:root symbolic link")
        )
        self.assertTrue(any(hit.offense_name == "king_not_regular" for hit in hits))

    def test_root_dir_770_is_flagged(self) -> None:
        hits = Poller(DummySSH())._detect_violations(_violation_snap(ROOT_DIR="770"))
        self.assertTrue(any(hit.offense_name == "root_dir_perm_changed" for hit in hits))

    def test_root_dir_700_is_not_flagged(self) -> None:
        hits = Poller(DummySSH())._detect_violations(_violation_snap(ROOT_DIR="700"))
        self.assertFalse(any(hit.offense_name == "root_dir_perm_changed" for hit in hits))

    def test_cron_king_token_matches_case_insensitively(self) -> None:
        for cron in ("* * * * * echo KING", "# spKING", "reKing things"):
            with self.subTest(cron=cron):
                hits = Poller(DummySSH())._detect_violations(_violation_snap(CRON=cron))
                self.assertTrue(any(hit.offense_name == "cron_king_persistence" for hit in hits))

    def test_cron_without_king_is_not_flagged(self) -> None:
        hits = Poller(DummySSH())._detect_violations(
            _violation_snap(CRON="* * * * * /usr/local/bin/backup.sh")
        )
        self.assertFalse(any(hit.offense_name == "cron_king_persistence" for hit in hits))

    def test_watchdog_inotify_token_is_flagged(self) -> None:
        hits = Poller(DummySSH())._detect_violations(
            _violation_snap(PROCS="root 42 0.0 inotifywait /root/king.txt")
        )
        self.assertTrue(any(hit.offense_name == "watchdog_process" for hit in hits))

    def test_watchdog_fswatch_token_is_flagged(self) -> None:
        hits = Poller(DummySSH())._detect_violations(
            _violation_snap(PROCS="root 43 0.0 fswatch -o /root/king.txt")
        )
        self.assertTrue(any(hit.offense_name == "watchdog_process" for hit in hits))

    def test_immutable_without_i_flag_is_not_flagged(self) -> None:
        # lsattr output like ``--------------- /root/king.txt`` has no
        # ``i`` flag — no violation should fire.
        hits = Poller(DummySSH())._detect_violations(
            _violation_snap(IMMUTABLE="--------------- /root/king.txt")
        )
        self.assertFalse(any(hit.offense_name == "king_immutable" for hit in hits))

    def test_immutable_as_isolated_word_is_flagged(self) -> None:
        # The current detector looks for the substring ``" i "`` (space-i-
        # space) inside the immutable blob. That fires when an ``i`` sits
        # on its own as a word — e.g. the hypothetical string ``"file
        # has i flag"`` — but does NOT fire on real ``lsattr`` output like
        # ``"----i---------- /root/king.txt"`` where the ``i`` is embedded
        # in a dash-run with no surrounding whitespace. The two tests
        # below pin both halves of that behavior so a future fix to the
        # detector (see docs/TESTING_AUDIT.md §2.4 L-series) has an
        # explicit contract to satisfy.
        hits = Poller(DummySSH())._detect_violations(
            _violation_snap(IMMUTABLE="file has i flag set")
        )
        self.assertTrue(any(hit.offense_name == "king_immutable" for hit in hits))

    def test_immutable_flag_embedded_in_lsattr_dashes_is_currently_missed(self) -> None:
        # KNOWN BUG (not a test bug): real lsattr output — for example
        # ``----i---------- /root/king.txt`` — has the ``i`` flag embedded
        # between dashes with no surrounding whitespace, so the
        # ``" i " in f" {immutable} "`` check silently returns False and
        # no violation is recorded. This test documents the present
        # behavior so a refactor that accidentally "fixes" the detector
        # in an unintended way (e.g. a case-insensitive substring check
        # that also matches ``Malicious`` or ``initialize``) is caught
        # too. The intended fix is to parse the flag field before the
        # filename and look for ``i`` in it; that is tracked in the
        # design audit as part of the rule-engine rewrite.
        hits = Poller(DummySSH())._detect_violations(
            _violation_snap(IMMUTABLE="----i---------- /root/king.txt")
        )
        self.assertFalse(any(hit.offense_name == "king_immutable" for hit in hits))

    def test_king_section_file_missing_is_flagged(self) -> None:
        # ``FILE_MISSING`` in the KING section means /root/king.txt is
        # gone entirely — that is always a violation regardless of
        # whether the scheduler has an authoritative owner.
        hits = Poller(DummySSH())._detect_violations(_violation_snap(KING="FILE_MISSING"))
        self.assertTrue(any(hit.offense_name == "king_deleted" for hit in hits))


# ---------------------------------------------------------------------------
# Edge cases for extract_sha256 / extract_sha256_or_missing.
# ---------------------------------------------------------------------------
class ExtractSha256EdgeCases(unittest.TestCase):
    def test_valid_lowercase_64_hex(self) -> None:
        digest = "a" * 64
        self.assertEqual(Poller.extract_sha256(digest), digest)

    def test_uppercase_input_is_lowercased(self) -> None:
        digest_upper = "A" * 64
        self.assertEqual(Poller.extract_sha256(digest_upper), "a" * 64)

    def test_mixed_case_is_lowercased(self) -> None:
        digest = "AbCdEf0123456789" * 4
        self.assertEqual(Poller.extract_sha256(digest), digest.lower())

    def test_wrong_length_63_returns_none(self) -> None:
        self.assertIsNone(Poller.extract_sha256("a" * 63))

    def test_wrong_length_65_returns_none(self) -> None:
        self.assertIsNone(Poller.extract_sha256("a" * 65))

    def test_non_hex_character_returns_none(self) -> None:
        self.assertIsNone(Poller.extract_sha256("g" * 64))

    def test_empty_string_returns_none(self) -> None:
        self.assertIsNone(Poller.extract_sha256(""))

    def test_leading_whitespace_is_handled(self) -> None:
        # First line is stripped, first token is extracted.
        digest = "a" * 64
        self.assertEqual(Poller.extract_sha256(f"  {digest}  /etc/shadow"), digest)

    def test_or_missing_returns_sentinel_when_none(self) -> None:
        self.assertEqual(Poller.extract_sha256_or_missing(""), MISSING_HASH)

    def test_or_missing_returns_digest_when_present(self) -> None:
        digest = "a" * 64
        self.assertEqual(Poller.extract_sha256_or_missing(digest), digest)


# ---------------------------------------------------------------------------
# Edge cases for stable_ports_signature.
# ---------------------------------------------------------------------------
class StablePortsSignatureEdgeCases(unittest.TestCase):
    def test_empty_string_returns_none(self) -> None:
        self.assertIsNone(Poller.stable_ports_signature(""))

    def test_only_header_returns_none(self) -> None:
        self.assertIsNone(Poller.stable_ports_signature("State Recv-Q Send-Q Local Address:Port Peer"))

    def test_only_loopback_listeners_returns_none(self) -> None:
        # Every matched entry points to a loopback address and is
        # filtered out; the signature must be None, NOT the hash of the
        # empty string.
        blob = """State  Recv-Q Send-Q Local Address:Port Peer Address:Port
LISTEN 0 100 127.0.0.1:8005 *:*
LISTEN 0 100 [::1]:8006 *:*
LISTEN 0 100 127.0.0.11:46209 *:*
"""
        self.assertIsNone(Poller.stable_ports_signature(blob))

    def test_duplicate_entries_produce_same_signature(self) -> None:
        first = """State
LISTEN 0 100 *:8080 *:*
LISTEN 0 100 *:8080 *:*
"""
        second = """State
LISTEN 0 100 *:8080 *:*
"""
        self.assertEqual(
            Poller.stable_ports_signature(first),
            Poller.stable_ports_signature(second),
        )

    def test_distinct_ports_produce_distinct_signatures(self) -> None:
        blob_a = "State\nLISTEN 0 100 *:8080 *:*\n"
        blob_b = "State\nLISTEN 0 100 *:9090 *:*\n"
        self.assertNotEqual(
            Poller.stable_ports_signature(blob_a),
            Poller.stable_ports_signature(blob_b),
        )


# ---------------------------------------------------------------------------
# Edge cases for _parse_snapshots (the multi-variant block parser).
# ---------------------------------------------------------------------------
class ParseSnapshotsEdgeCases(unittest.TestCase):
    def test_output_with_no_variant_headers_returns_empty(self) -> None:
        poller = Poller(DummySSH())
        self.assertEqual(poller._parse_snapshots("host-a", "random output\nno markers here"), [])

    def test_unclosed_variant_block_still_produces_snapshot(self) -> None:
        # An SSH session that gets SIGINT'd mid-probe will truncate the
        # output before ``===END_VARIANT===``. The parser must still
        # produce a snapshot for whatever it got, flagged by the
        # downstream ``status`` check.
        output = "\n".join(
            [
                "===VARIANT:A===",
                "===KING===",
                "Team Alpha",
                "===KING_STAT===",
                "1000 644 root:root regular file",
            ]
        )
        snaps = Poller(DummySSH())._parse_snapshots("host-a", output)
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0].variant, "A")
        self.assertEqual(snaps[0].king, "Team Alpha")

    def test_variant_without_sections_is_failed(self) -> None:
        # If the probe enters a variant block but produces no sections at
        # all before end-of-output, the snapshot is marked failed so the
        # scorer filters it out.
        output = "\n".join(["===VARIANT:A===", "===END_VARIANT==="])
        snaps = Poller(DummySSH())._parse_snapshots("host-a", output)
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0].status, "failed")

    def test_container_not_found_error_marks_snapshot_failed(self) -> None:
        output = "\n".join(
            ["===VARIANT:A===", "===ERROR===", "CONTAINER_NOT_FOUND", "===END_VARIANT==="]
        )
        snaps = Poller(DummySSH())._parse_snapshots("host-a", output)
        self.assertEqual(snaps[0].status, "failed")

    def test_two_consecutive_variant_a_blocks_yield_two_snapshots(self) -> None:
        # This is a pathological probe output (variants should be unique
        # per host) but the parser shouldn't crash or dedupe — it should
        # let the caller see both and decide.
        output = "\n".join(
            [
                "===VARIANT:A===",
                "===KING===",
                "Team Alpha",
                "===END_VARIANT===",
                "===VARIANT:A===",
                "===KING===",
                "Team Beta",
                "===END_VARIANT===",
            ]
        )
        snaps = Poller(DummySSH())._parse_snapshots("host-a", output)
        self.assertEqual(len(snaps), 2)
        self.assertEqual([snap.king for snap in snaps], ["Team Alpha", "Team Beta"])
