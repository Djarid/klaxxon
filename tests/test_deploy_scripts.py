"""Failing TDD tests for the deploy-simplify feature.

Tests every acceptance criterion from .Claude/plans/deploy-simplify.md
that is file-content-testable.  These tests assert on the CONTENT of files
in the repository — they do NOT SSH to any host or run destructive commands.

All tests MUST FAIL until the implementation described in the spec is applied.

References:
    .Claude/plans/deploy-simplify.md — requirements REQ-1 … REQ-17 and
    acceptance criteria AC-1 … AC-25.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest

# --------------------------------------------------------------------------
# Repository root (absolute, so tests work from any cwd)
# --------------------------------------------------------------------------

REPO = Path(__file__).resolve().parent.parent

SETUP_API = REPO / "deploy" / "setup-api.sh"
REDEPLOY_API = REPO / "deploy" / "redeploy-api.sh"
DECOMMISSION_WEB = REPO / "deploy" / "decommission-web.sh"
TRAEFIK_YML = REPO / "deploy" / "traefik" / "klaxxon.yml"
CONFIG_PY = REPO / "src" / "config.py"
MAIN_PY = REPO / "src" / "main.py"
ENV_TEMPLATE = REPO / ".env.template"

# Files that must NOT exist after the cleanup
GEN_CERTS = REPO / "deploy" / "gen-certs.sh"
SETUP_WEB = REPO / "deploy" / "setup-web.sh"
CADDYFILE = REPO / "web" / "Caddyfile"


# ==========================================================================
# AC-1  setup-api.sh — no TLS/cert references
# ==========================================================================


class TestSetupApiNoCertReferences:
    """REQ-4 / AC-1: setup-api.sh must not reference cert/TLS artefacts."""

    def _content(self) -> str:
        assert SETUP_API.exists(), f"{SETUP_API} does not exist"
        return SETUP_API.read_text()

    def test_no_ssl_certfile_flag(self):
        """AC-1: --ssl-certfile must not appear in setup-api.sh (REQ-2, REQ-4)."""
        content = self._content()
        assert "--ssl-certfile" not in content, (
            "setup-api.sh still contains --ssl-certfile; TLS flags must be removed"
        )

    def test_no_ssl_keyfile_flag(self):
        """AC-1: --ssl-keyfile must not appear in setup-api.sh (REQ-2, REQ-4)."""
        content = self._content()
        assert "--ssl-keyfile" not in content, (
            "setup-api.sh still contains --ssl-keyfile; TLS flags must be removed"
        )

    def test_no_api_crt_reference(self):
        """AC-1: api.crt must not appear in setup-api.sh (REQ-4)."""
        content = self._content()
        assert "api.crt" not in content, (
            "setup-api.sh still references api.crt; cert block must be removed"
        )

    def test_no_api_key_reference(self):
        """AC-1: api.key must not appear in setup-api.sh (REQ-4)."""
        content = self._content()
        assert "api.key" not in content, (
            "setup-api.sh still references api.key; cert block must be removed"
        )

    def test_no_ca_crt_reference(self):
        """AC-1: ca.crt must not appear in setup-api.sh (REQ-4)."""
        content = self._content()
        assert "ca.crt" not in content, (
            "setup-api.sh still references ca.crt; cert block must be removed"
        )

    def test_no_gen_certs_reference(self):
        """AC-1: gen-certs must not be mentioned in setup-api.sh (REQ-4)."""
        content = self._content()
        assert "gen-certs" not in content, (
            "setup-api.sh still references gen-certs; cert block must be removed"
        )

    def test_no_cert_dir_variable(self):
        """AC-1: CERT_DIR variable must not appear in setup-api.sh (REQ-4)."""
        content = self._content()
        assert "CERT_DIR" not in content, (
            "setup-api.sh still defines CERT_DIR; cert configuration must be removed"
        )


# ==========================================================================
# AC-2  setup-api.sh — user creation before first chown
# ==========================================================================


class TestSetupApiUserBeforeChown:
    """REQ-3 / AC-2: useradd line must precede ANY chown klaxxon:klaxxon line."""

    def test_useradd_before_chown(self):
        """AC-2: klaxxon user must be created BEFORE any chown klaxxon:klaxxon."""
        assert SETUP_API.exists(), f"{SETUP_API} does not exist"
        content = SETUP_API.read_text()
        lines = content.splitlines()

        useradd_line = None
        first_chown_line = None

        for i, line in enumerate(lines, start=1):
            if useradd_line is None and "useradd" in line and "klaxxon" in line:
                useradd_line = i
            if first_chown_line is None and re.search(
                r"chown\b.*klaxxon:klaxxon", line
            ):
                first_chown_line = i

        assert useradd_line is not None, (
            "setup-api.sh has no 'useradd ... klaxxon' line"
        )
        assert first_chown_line is not None, (
            "setup-api.sh has no 'chown klaxxon:klaxxon' line"
        )
        assert useradd_line < first_chown_line, (
            f"useradd klaxxon is at line {useradd_line} but first chown is at "
            f"line {first_chown_line}; user must be created BEFORE any chown"
        )


# ==========================================================================
# AC-3  setup-api.sh — uvicorn on port 8000, no --ssl-* flags
# ==========================================================================


class TestSetupApiUvicornCommand:
    """REQ-1 / REQ-2 / AC-3: uvicorn ExecStart uses --port 8000, no TLS flags."""

    def _content(self) -> str:
        assert SETUP_API.exists(), f"{SETUP_API} does not exist"
        return SETUP_API.read_text()

    def test_uvicorn_port_8000(self):
        """AC-3: uvicorn ExecStart must contain --port 8000."""
        content = self._content()
        assert "--port 8000" in content, (
            "setup-api.sh uvicorn ExecStart does not contain '--port 8000'; "
            "REQ-1 and REQ-2 require plain HTTP on port 8000"
        )

    def test_uvicorn_no_port_8443(self):
        """AC-3: uvicorn must NOT use port 8443."""
        content = self._content()
        assert "--port 8443" not in content, (
            "setup-api.sh uvicorn ExecStart still uses --port 8443; "
            "must be changed to 8000"
        )

    def test_uvicorn_no_ssl_flags(self):
        """AC-3: uvicorn ExecStart must contain no --ssl-* flags."""
        content = self._content()
        assert "--ssl-" not in content, (
            "setup-api.sh uvicorn ExecStart still contains --ssl-* flags; "
            "plain HTTP requires no TLS flags"
        )


# ==========================================================================
# AC-4  setup-api.sh — web/ included in deploy tarball
# ==========================================================================


class TestSetupApiWebInTarball:
    """REQ-6 / AC-4: tar command must include web/ directory."""

    def test_tar_includes_web_dir(self):
        """AC-4: deploy tarball must include web/ so FastAPI StaticFiles can serve SPA."""
        assert SETUP_API.exists(), f"{SETUP_API} does not exist"
        content = SETUP_API.read_text()

        # Find the tar -czf line and check it includes web/
        tar_lines = [
            line.strip()
            for line in content.splitlines()
            if "tar" in line and "-czf" in line
        ]
        # Also look across continuation lines (backslash-continued commands)
        # by searching raw content around tar -czf
        assert tar_lines, "setup-api.sh has no 'tar -czf' command"

        # The tar command may span multiple lines via backslash continuation.
        # Collapse continuation lines and search the full command block.
        collapsed = re.sub(r"\\\n\s*", " ", content)
        tar_match = re.search(r"tar\s+-czf[^\n]*", collapsed)
        assert tar_match is not None, (
            "Could not locate tar -czf command in setup-api.sh"
        )

        tar_cmd = tar_match.group(0)
        assert "web/" in tar_cmd or re.search(r"\bweb\b", tar_cmd), (
            f"tar command does not include 'web/': {tar_cmd!r}\n"
            "REQ-6 requires web/ to be bundled in the deploy tarball"
        )


# ==========================================================================
# AC-5  setup-api.sh — no sqlite3 CLI invocations
# ==========================================================================


class TestSetupApiNoSqlite3CLI:
    """REQ-5 / AC-5: sqlite3 CLI binary must not be invoked in setup-api.sh."""

    def test_no_sqlite3_cli_binary_invocation(self):
        """AC-5: 'sqlite3 <path>' invocations must not appear in setup-api.sh.

        The spec (REQ-5) says all DB operations must use Python's sqlite3 module.
        The sqlite3 CLI is not installed in the LXC.  We allow 'apt-get install sqlite3'
        to be absent but must not find any CLI invocation like 'sqlite3 /path/...'.
        """
        assert SETUP_API.exists(), f"{SETUP_API} does not exist"
        content = SETUP_API.read_text()

        # Match `sqlite3 ` followed by a path character (/ or $) or a flag (-)
        # This catches: sqlite3 /opt/... or sqlite3 $DB or sqlite3 -init ...
        # Exclude comment lines and python3 -c '...sqlite3...' style invocations.
        bad_lines = []
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Look for bare sqlite3 CLI invocations (not inside python -c strings)
            if re.search(r"\bsqlite3\s+[/$-]", stripped):
                # Exclude if it's inside a python -c or python3 -c call
                if not re.search(r"python3?\s+-c\b", stripped):
                    bad_lines.append(stripped)

        assert not bad_lines, (
            "setup-api.sh invokes the sqlite3 CLI binary (REQ-5 forbids this):\n"
            + "\n".join(f"  {l}" for l in bad_lines)
        )


# ==========================================================================
# AC-6  setup-api.sh — Traefik config push
# ==========================================================================


class TestSetupApiTraefikPush:
    """REQ-7 / AC-6: setup-api.sh must push Traefik config and reload Traefik."""

    def _content(self) -> str:
        assert SETUP_API.exists(), f"{SETUP_API} does not exist"
        return SETUP_API.read_text()

    def test_copies_traefik_klaxxon_yml(self):
        """AC-6: setup-api.sh must copy deploy/traefik/klaxxon.yml to the Traefik LXC."""
        content = self._content()
        assert "traefik/klaxxon.yml" in content or "klaxxon.yml" in content, (
            "setup-api.sh does not reference traefik/klaxxon.yml; "
            "REQ-7 requires pushing the Traefik config file"
        )

    def test_reloads_traefik(self):
        """AC-6: setup-api.sh must reload or restart Traefik after config push."""
        content = self._content()
        # Accept: systemctl reload traefik, systemctl restart traefik, or similar
        has_traefik_reload = bool(
            re.search(r"(reload|restart)\s+traefik", content)
            or re.search(r"traefik[^\n]*(reload|restart)", content)
        )
        assert has_traefik_reload, (
            "setup-api.sh does not reload Traefik after pushing config; "
            "REQ-7 requires 'systemctl reload traefik' (or equivalent)"
        )

    def test_traefik_push_warns_but_does_not_fail(self):
        """AC-6: Traefik config push must be wrapped so failure is a warning, not abort.

        The spec says: 'warn but not fail if it cannot push the Traefik config'.
        This is typically implemented with '|| true' or a sub-shell that doesn't exit.
        """
        content = self._content()
        # The Traefik section should either use '|| true' around the push/reload
        # or be wrapped in a conditional block.  We look for a broad pattern.
        has_soft_fail = bool(
            re.search(r"traefik.*\|\|\s*true", content, re.IGNORECASE)
            or re.search(r"\|\|\s*true.*traefik", content, re.IGNORECASE)
            # or the whole block is in a sub-shell / if block that doesn't abort
            or re.search(r"traefik.*2>/dev/null", content, re.IGNORECASE)
        )
        assert has_soft_fail, (
            "setup-api.sh does not gracefully handle Traefik push failure "
            "(expected '|| true' or equivalent around Traefik section); "
            "REQ-7 / edge-case: 'Traefik LXC unreachable' must not abort deploy"
        )


# ==========================================================================
# AC-7  setup-api.sh — final echo shows http:// port 8000
# ==========================================================================


class TestSetupApiFinalEcho:
    """REQ-1 / AC-7: Final summary line must show http:// and port 8000."""

    def test_final_echo_uses_http_not_https(self):
        """AC-7: Final echo/print must reference http:// not https://."""
        assert SETUP_API.exists(), f"{SETUP_API} does not exist"
        content = SETUP_API.read_text()

        # Find echo lines that contain an IP or hostname
        echo_lines = [
            l.strip()
            for l in content.splitlines()
            if re.search(r"\becho\b", l) and "192.168.1.11" in l
        ]
        assert echo_lines, "setup-api.sh has no echo line containing '192.168.1.11'"

        for line in echo_lines:
            assert "https://" not in line, (
                f"setup-api.sh final echo still shows https://: {line!r}\n"
                "AC-7 requires http:// in the final summary"
            )

    def test_final_echo_uses_port_8000(self):
        """AC-7: Final echo/print must reference port 8000 not 8443."""
        assert SETUP_API.exists(), f"{SETUP_API} does not exist"
        content = SETUP_API.read_text()

        echo_lines = [
            l.strip()
            for l in content.splitlines()
            if re.search(r"\becho\b", l) and "192.168.1.11" in l
        ]
        assert echo_lines, "setup-api.sh has no echo line containing '192.168.1.11'"

        for line in echo_lines:
            assert "8443" not in line, (
                f"setup-api.sh final echo still shows port 8443: {line!r}\n"
                "AC-7 requires port 8000 in the final summary"
            )
            assert "8000" in line, (
                f"setup-api.sh final echo does not show port 8000: {line!r}"
            )


# ==========================================================================
# AC-17 — also check setup-api.sh has no TLS_CERT_PATH / TLS_KEY_PATH sed lines
# ==========================================================================


class TestSetupApiNoTlsEnvSed:
    """REQ-17 / AC-17: setup-api.sh must not sed TLS_CERT_PATH or TLS_KEY_PATH."""

    def test_no_tls_cert_path_sed(self):
        """REQ-17: TLS_CERT_PATH sed line must be removed from setup-api.sh."""
        assert SETUP_API.exists(), f"{SETUP_API} does not exist"
        content = SETUP_API.read_text()
        assert "TLS_CERT_PATH" not in content, (
            "setup-api.sh still contains TLS_CERT_PATH sed; REQ-17 requires removal"
        )

    def test_no_tls_key_path_sed(self):
        """REQ-17: TLS_KEY_PATH sed line must be removed from setup-api.sh."""
        assert SETUP_API.exists(), f"{SETUP_API} does not exist"
        content = SETUP_API.read_text()
        assert "TLS_KEY_PATH" not in content, (
            "setup-api.sh still contains TLS_KEY_PATH sed; REQ-17 requires removal"
        )


# ==========================================================================
# Redeploy script — AC-8 … AC-11
# ==========================================================================


class TestRedeployApiExists:
    """REQ-9 / AC-8: deploy/redeploy-api.sh must exist and be executable."""

    def test_redeploy_script_exists(self):
        """AC-8: deploy/redeploy-api.sh must exist."""
        assert REDEPLOY_API.exists(), (
            f"{REDEPLOY_API} does not exist; REQ-9 requires creating redeploy-api.sh"
        )

    def test_redeploy_script_is_executable(self):
        """AC-8: deploy/redeploy-api.sh must be executable."""
        assert REDEPLOY_API.exists(), f"{REDEPLOY_API} does not exist"
        mode = REDEPLOY_API.stat().st_mode
        is_exec = bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
        assert is_exec, (
            f"{REDEPLOY_API} is not executable (mode={oct(mode)}); "
            "run 'chmod +x deploy/redeploy-api.sh'"
        )


class TestRedeployApiCallsSetupApi:
    """REQ-9 / AC-8: redeploy-api.sh must call setup-api.sh."""

    def test_calls_setup_api(self):
        """AC-8: redeploy-api.sh must invoke setup-api.sh."""
        assert REDEPLOY_API.exists(), f"{REDEPLOY_API} does not exist"
        content = REDEPLOY_API.read_text()
        assert "setup-api.sh" in content, (
            "redeploy-api.sh does not call setup-api.sh; "
            "REQ-9(b) requires running setup-api.sh to recreate the LXC"
        )


class TestRedeployApiDumpBeforeSetup:
    """REQ-9 / AC-9: DB dump must happen BEFORE setup-api.sh runs."""

    def test_pct_pull_before_setup_api(self):
        """AC-9: pct pull (DB extraction) must appear before setup-api.sh invocation."""
        assert REDEPLOY_API.exists(), f"{REDEPLOY_API} does not exist"
        content = REDEPLOY_API.read_text()
        lines = content.splitlines()

        pct_pull_line = None
        setup_api_line = None
        for i, line in enumerate(lines, start=1):
            if pct_pull_line is None and "pct pull" in line:
                pct_pull_line = i
            if setup_api_line is None and "setup-api.sh" in line:
                setup_api_line = i

        assert pct_pull_line is not None, (
            "redeploy-api.sh has no 'pct pull' command; "
            "REQ-9(a) requires dumping the live DB before setup-api.sh"
        )
        assert setup_api_line is not None, (
            "redeploy-api.sh has no 'setup-api.sh' invocation"
        )
        assert pct_pull_line < setup_api_line, (
            f"pct pull is at line {pct_pull_line} but setup-api.sh is at "
            f"line {setup_api_line}; DB dump must happen BEFORE setup-api.sh (AC-9)"
        )


class TestRedeployApiRestoreAfterSetup:
    """REQ-9 / AC-10: DB restore must happen AFTER setup-api.sh runs."""

    def test_pct_push_after_setup_api(self):
        """AC-10: pct push (DB restore) must appear after setup-api.sh invocation."""
        assert REDEPLOY_API.exists(), f"{REDEPLOY_API} does not exist"
        content = REDEPLOY_API.read_text()
        lines = content.splitlines()

        setup_api_line = None
        pct_push_line = None
        for i, line in enumerate(lines, start=1):
            if setup_api_line is None and "setup-api.sh" in line:
                setup_api_line = i
            if "pct push" in line:
                pct_push_line = i  # take the LAST pct push (restore step)

        assert setup_api_line is not None, (
            "redeploy-api.sh has no 'setup-api.sh' invocation"
        )
        assert pct_push_line is not None, (
            "redeploy-api.sh has no 'pct push' command; "
            "REQ-9(c) requires restoring the DB after setup-api.sh"
        )
        assert pct_push_line > setup_api_line, (
            f"pct push is at line {pct_push_line} but setup-api.sh is at "
            f"line {setup_api_line}; DB restore must happen AFTER setup-api.sh (AC-10)"
        )


class TestRedeployApiFirstDeployHandling:
    """REQ-10 / AC-11: redeploy-api.sh must handle first deploy (no existing DB)."""

    def test_conditional_before_dump(self):
        """AC-11: DB dump must be conditional — graceful when no DB exists."""
        assert REDEPLOY_API.exists(), f"{REDEPLOY_API} does not exist"
        content = REDEPLOY_API.read_text()

        # The spec shows: `if ssh ... pct exec ... test -f $DB_PATH`
        # Accept any conditional guard: 'if', 'test -f', '[ -f', or '2>/dev/null'
        has_guard = bool(
            re.search(r"test\s+-f\b", content)
            or re.search(r"\[\s+-f\b", content)
            or re.search(r"if.*pct.*DB", content, re.IGNORECASE)
        )
        assert has_guard, (
            "redeploy-api.sh has no conditional guard before the DB dump; "
            "REQ-10 requires graceful handling when no existing DB is found "
            "(first-deploy case)"
        )

    def test_no_unconditional_error_exit_on_missing_db(self):
        """AC-11: redeploy-api.sh must not 'exit 1' unconditionally on missing DB."""
        assert REDEPLOY_API.exists(), f"{REDEPLOY_API} does not exist"
        content = REDEPLOY_API.read_text()

        # The pct pull / scp step must be inside a conditional block.
        # Simple check: there should be an 'else' or fallback after the dump section.
        has_else_or_fallback = bool(
            re.search(r"\belse\b", content)
            or re.search(r'BACKUP_FILE=""', content)
            or re.search(r"Skipping backup", content, re.IGNORECASE)
        )
        assert has_else_or_fallback, (
            "redeploy-api.sh has no 'else' / fallback path for the missing-DB case; "
            "REQ-10 requires graceful skip when no DB exists (first deploy)"
        )


# ==========================================================================
# Decommission script — AC-12 … AC-13
# ==========================================================================


class TestDecommissionWebExists:
    """REQ-11 / AC-12: deploy/decommission-web.sh must exist and be executable."""

    def test_decommission_script_exists(self):
        """AC-12: deploy/decommission-web.sh must exist."""
        assert DECOMMISSION_WEB.exists(), (
            f"{DECOMMISSION_WEB} does not exist; "
            "REQ-11 requires creating deploy/decommission-web.sh"
        )

    def test_decommission_script_is_executable(self):
        """AC-12: deploy/decommission-web.sh must be executable."""
        assert DECOMMISSION_WEB.exists(), f"{DECOMMISSION_WEB} does not exist"
        mode = DECOMMISSION_WEB.stat().st_mode
        is_exec = bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
        assert is_exec, f"{DECOMMISSION_WEB} is not executable (mode={oct(mode)})"


class TestDecommissionWebContent:
    """REQ-11 / AC-12: decommission-web.sh must stop and destroy LXC 112."""

    def _content(self) -> str:
        assert DECOMMISSION_WEB.exists(), f"{DECOMMISSION_WEB} does not exist"
        return DECOMMISSION_WEB.read_text()

    def test_stops_lxc_112(self):
        """AC-12: script must run pct stop 112."""
        content = self._content()
        # Accept 'pct stop 112' or 'pct stop $CTID' where CTID=112
        has_stop = bool(
            re.search(r"pct\s+stop\s+112\b", content)
            or (
                re.search(r"CTID=112\b", content)
                and re.search(r"pct\s+stop\b", content)
            )
        )
        assert has_stop, (
            "decommission-web.sh does not contain 'pct stop 112'; "
            "REQ-11 requires stopping LXC 112"
        )

    def test_destroys_lxc_112_with_purge(self):
        """AC-12: script must run pct destroy 112 --purge."""
        content = self._content()
        has_destroy = bool(
            re.search(r"pct\s+destroy\s+112\b", content)
            or (
                re.search(r"CTID=112\b", content)
                and re.search(r"pct\s+destroy\b", content)
            )
        )
        assert has_destroy, (
            "decommission-web.sh does not contain 'pct destroy 112'; "
            "REQ-11 requires destroying LXC 112"
        )
        assert "--purge" in content, (
            "decommission-web.sh missing '--purge' flag on pct destroy; "
            "spec requires 'pct destroy 112 --purge'"
        )

    def test_handles_missing_lxc_gracefully(self):
        """AC-13: script must not hard-fail when LXC 112 does not exist."""
        content = self._content()
        # Expect a conditional check before destroy: `if pct status ...` or similar
        has_guard = bool(
            re.search(r"if\s+ssh.*pct\s+status", content)
            or re.search(r"pct\s+status\s+\$?CTID", content)
            or re.search(r"pct\s+status\s+112", content)
            or re.search(r"does not exist", content, re.IGNORECASE)
        )
        assert has_guard, (
            "decommission-web.sh has no conditional check before destroying LXC 112; "
            "AC-13 requires graceful exit when LXC 112 does not exist"
        )


# ==========================================================================
# Traefik YAML — AC-14 … AC-16
# ==========================================================================


class TestTraefikYml:
    """REQ-8 / AC-14…16: deploy/traefik/klaxxon.yml must exist with correct content."""

    def test_traefik_yml_exists(self):
        """AC-14: deploy/traefik/klaxxon.yml must exist."""
        assert TRAEFIK_YML.exists(), (
            f"{TRAEFIK_YML} does not exist; "
            "REQ-8 requires creating deploy/traefik/klaxxon.yml"
        )

    def test_service_url_is_http_port_8000(self):
        """AC-14: Traefik config must route to http://192.168.1.11:8000."""
        assert TRAEFIK_YML.exists(), f"{TRAEFIK_YML} does not exist"
        content = TRAEFIK_YML.read_text()
        assert "http://192.168.1.11:8000" in content, (
            "deploy/traefik/klaxxon.yml does not contain 'http://192.168.1.11:8000'; "
            "REQ-8 requires routing to plain HTTP port 8000 (not https/8443)"
        )

    def test_no_https_api_url(self):
        """AC-15: deploy/traefik/klaxxon.yml must not contain https://192.168.1.11."""
        assert TRAEFIK_YML.exists(), f"{TRAEFIK_YML} does not exist"
        content = TRAEFIK_YML.read_text()
        assert "https://192.168.1.11" not in content, (
            "deploy/traefik/klaxxon.yml still contains 'https://192.168.1.11'; "
            "AC-15 requires no HTTPS backend URLs"
        )

    def test_no_port_8443_in_traefik_config(self):
        """AC-15: port 8443 must not appear in the Traefik service URL."""
        assert TRAEFIK_YML.exists(), f"{TRAEFIK_YML} does not exist"
        content = TRAEFIK_YML.read_text()
        assert "8443" not in content, (
            "deploy/traefik/klaxxon.yml still references port 8443; "
            "the backend URL must be port 8000"
        )

    def test_router_rule_contains_hostname(self):
        """AC-16: Traefik router rule must reference klaxxon.horizons-call.com."""
        assert TRAEFIK_YML.exists(), f"{TRAEFIK_YML} does not exist"
        content = TRAEFIK_YML.read_text()
        assert "klaxxon.horizons-call.com" in content, (
            "deploy/traefik/klaxxon.yml does not contain 'klaxxon.horizons-call.com'; "
            "AC-16 requires the router to match on the correct hostname"
        )

    def test_traefik_yml_is_valid_yaml(self):
        """AC-14: deploy/traefik/klaxxon.yml must be parseable YAML."""
        assert TRAEFIK_YML.exists(), f"{TRAEFIK_YML} does not exist"
        try:
            import yaml

            data = yaml.safe_load(TRAEFIK_YML.read_text())
        except Exception as exc:
            pytest.fail(f"deploy/traefik/klaxxon.yml is not valid YAML: {exc}")
        assert data is not None, (
            "deploy/traefik/klaxxon.yml parses to None (empty file?)"
        )

    def test_traefik_yml_has_http_services_section(self):
        """AC-14: Traefik YAML must have an http.services section with a server URL."""
        assert TRAEFIK_YML.exists(), f"{TRAEFIK_YML} does not exist"
        import yaml

        data = yaml.safe_load(TRAEFIK_YML.read_text())
        assert isinstance(data, dict), "Traefik YAML root must be a mapping"
        assert "http" in data, "Traefik YAML missing 'http' key"
        assert "services" in data["http"], (
            "Traefik YAML missing 'http.services' section"
        )

        # Drill down to the server URL
        services = data["http"]["services"]
        urls = []
        for svc_name, svc_data in services.items():
            if isinstance(svc_data, dict):
                lb = svc_data.get("loadBalancer", {})
                servers = lb.get("servers", [])
                for srv in servers:
                    if "url" in srv:
                        urls.append(srv["url"])

        assert any("192.168.1.11:8000" in u for u in urls), (
            f"No service URL containing '192.168.1.11:8000' found in services; "
            f"found: {urls}"
        )


# ==========================================================================
# Config cleanup — AC-17 … AC-20
# ==========================================================================


class TestConfigPyCleanup:
    """REQ-14 / REQ-15 / REQ-16 / AC-17…19: src/config.py must drop TLS fields."""

    def _content(self) -> str:
        assert CONFIG_PY.exists(), f"{CONFIG_PY} does not exist"
        return CONFIG_PY.read_text()

    def test_no_tls_cert_field(self):
        """AC-17: AppConfig must not have a tls_cert field."""
        content = self._content()
        # Match field definitions: 'tls_cert:' or 'tls_cert ='
        assert not re.search(r"\btls_cert\s*[=:]", content), (
            "src/config.py still defines 'tls_cert' field in AppConfig; "
            "REQ-14 requires removing TLS fields"
        )

    def test_no_tls_key_field(self):
        """AC-17: AppConfig must not have a tls_key field."""
        content = self._content()
        assert not re.search(r"\btls_key\s*[=:]", content), (
            "src/config.py still defines 'tls_key' field in AppConfig; "
            "REQ-14 requires removing TLS fields"
        )

    def test_api_port_default_is_8000(self):
        """AC-18: AppConfig.api_port default must be 8000, not 8443."""
        content = self._content()
        # Match: api_port: int = 8000
        assert re.search(r"api_port\s*:\s*int\s*=\s*8000", content), (
            "src/config.py still has api_port default != 8000; "
            "REQ-14 requires changing the default from 8443 to 8000"
        )

    def test_api_port_not_8443(self):
        """AC-18: api_port default must NOT be 8443."""
        content = self._content()
        assert not re.search(r"api_port\s*:\s*int\s*=\s*8443", content), (
            "src/config.py api_port default is still 8443; must be changed to 8000"
        )

    def test_load_config_no_tls_cert_path(self):
        """AC-19: load_config() must not load TLS_CERT_PATH from env."""
        content = self._content()
        assert "TLS_CERT_PATH" not in content, (
            "src/config.py still references TLS_CERT_PATH; "
            "REQ-16 requires removing TLS env var loading"
        )

    def test_load_config_no_tls_key_path(self):
        """AC-19: load_config() must not load TLS_KEY_PATH from env."""
        content = self._content()
        assert "TLS_KEY_PATH" not in content, (
            "src/config.py still references TLS_KEY_PATH; "
            "REQ-16 requires removing TLS env var loading"
        )


class TestEnvTemplateCleanup:
    """REQ-15 / AC-20: .env.template must not contain TLS_CERT_PATH or TLS_KEY_PATH."""

    def _content(self) -> str:
        assert ENV_TEMPLATE.exists(), f"{ENV_TEMPLATE} does not exist"
        return ENV_TEMPLATE.read_text()

    def test_no_tls_cert_path_entry(self):
        """AC-20: .env.template must not contain TLS_CERT_PATH."""
        content = self._content()
        assert "TLS_CERT_PATH" not in content, (
            ".env.template still contains TLS_CERT_PATH; "
            "REQ-15 requires removing TLS entries"
        )

    def test_no_tls_key_path_entry(self):
        """AC-20: .env.template must not contain TLS_KEY_PATH."""
        content = self._content()
        assert "TLS_KEY_PATH" not in content, (
            ".env.template still contains TLS_KEY_PATH; "
            "REQ-15 requires removing TLS entries"
        )


# ==========================================================================
# File deletion — AC-21 … AC-23
# ==========================================================================


class TestObsoleteFilesDeleted:
    """REQ-12 / AC-21…23: cert and Caddy files must be removed from the repo."""

    def test_gen_certs_sh_deleted(self):
        """AC-21: deploy/gen-certs.sh must not exist in the repo."""
        assert not GEN_CERTS.exists(), (
            f"{GEN_CERTS} still exists; "
            "REQ-12 requires deleting deploy/gen-certs.sh (certs no longer needed)"
        )

    def test_setup_web_sh_deleted(self):
        """AC-22: deploy/setup-web.sh must not exist in the repo."""
        assert not SETUP_WEB.exists(), (
            f"{SETUP_WEB} still exists; "
            "REQ-12 requires deleting deploy/setup-web.sh (LXC 112 decommissioned)"
        )

    def test_caddyfile_deleted(self):
        """AC-23: web/Caddyfile must not exist in the repo."""
        assert not CADDYFILE.exists(), (
            f"{CADDYFILE} still exists; "
            "REQ-12 requires deleting web/Caddyfile (Caddy is no longer used)"
        )


# ==========================================================================
# Application code preservation — AC-24
# ==========================================================================


class TestMainPyStaticFilesMount:
    """AC-24: src/main.py StaticFiles mount must be present and serve web/ dir."""

    def test_static_files_mount_exists(self):
        """AC-24: app.mount with StaticFiles must be present in src/main.py."""
        assert MAIN_PY.exists(), f"{MAIN_PY} does not exist"
        content = MAIN_PY.read_text()
        assert "StaticFiles" in content, (
            "src/main.py no longer imports or uses StaticFiles; "
            "AC-24 requires the SPA mount to remain unchanged"
        )

    def test_static_files_mounts_web_dir(self):
        """AC-24: StaticFiles must mount the web/ directory."""
        assert MAIN_PY.exists(), f"{MAIN_PY} does not exist"
        content = MAIN_PY.read_text()
        # Match: StaticFiles(directory=str(_web_dir) or directory="web" variants
        assert re.search(r"StaticFiles\(.*directory.*web", content), (
            "src/main.py StaticFiles mount does not reference the web/ directory; "
            "AC-24 requires serving SPA from web/"
        )

    def test_static_files_html_true(self):
        """AC-24: StaticFiles must use html=True for SPA routing."""
        assert MAIN_PY.exists(), f"{MAIN_PY} does not exist"
        content = MAIN_PY.read_text()
        assert "html=True" in content, (
            "src/main.py StaticFiles mount is missing html=True; "
            "AC-24 requires html=True for SPA index.html fallback"
        )

    def test_static_files_mounted_at_root(self):
        """AC-24: StaticFiles must be mounted at '/' so SPA is served from root."""
        assert MAIN_PY.exists(), f"{MAIN_PY} does not exist"
        content = MAIN_PY.read_text()
        # Match: app.mount("/", StaticFiles(...)
        assert re.search(r'app\.mount\(\s*["\'/][\s"\']*,\s*StaticFiles', content), (
            "src/main.py does not mount StaticFiles at '/'; "
            "AC-24 requires mounting at root for SPA serving"
        )

    def test_web_dir_is_guarded(self):
        """AC-24/edge-case: mount must be conditional on web/ directory existing."""
        assert MAIN_PY.exists(), f"{MAIN_PY} does not exist"
        content = MAIN_PY.read_text()
        # Spec says: 'if _web_dir.is_dir()' guard is already present
        assert re.search(r"is_dir\(\)", content), (
            "src/main.py StaticFiles mount has no is_dir() guard; "
            "the spec requires conditional mounting (edge case: SPA assets missing)"
        )


# ==========================================================================
# Decommission script — executable bit
# ==========================================================================


class TestSetupApiExecutable:
    """REQ — deploy scripts must be executable."""

    def test_setup_api_is_executable(self):
        """setup-api.sh must be executable (sanity check — should already pass)."""
        assert SETUP_API.exists(), f"{SETUP_API} does not exist"
        mode = SETUP_API.stat().st_mode
        is_exec = bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
        assert is_exec, f"deploy/setup-api.sh is not executable (mode={oct(mode)})"
