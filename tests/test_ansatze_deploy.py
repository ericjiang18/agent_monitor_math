"""Pin the ansatze.ai deployment bundle and the `create-admin` CLI.

The bundle (deploy/ansatze/) is installed by the owner with root; these tests
check the security-relevant contracts without root: the console holds no Kimi
key, registration stays closed, /sign-in lands on <mount>/login before the
static site, the broker port and /internal/* are never routed, and the admin
account can be created without opening HTTP registration.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from agent_monitor import auth, cli

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "deploy" / "ansatze"
MOUNT = "/proof-0123456789abcdef0123456789abcdef01234567"
UNITS = (
    "agent-monitor.service",
    "ansatze-kimi.service",
    "ansatze-news.service",
    "ansatze-news.timer",
    "ansatze-problems.service",
    "ansatze-problems.timer",
)


def _unit(name: str) -> str:
    return (BUNDLE / name).read_text(encoding="utf-8")


def _directives(text: str) -> list[tuple[str, str]]:
    pairs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "[")) or "=" not in line:
            continue
        key, _, value = line.partition("=")
        pairs.append((key, value))
    return pairs


def _environment(text: str) -> dict[str, str]:
    values = {}
    for key, value in _directives(text):
        if key == "Environment":
            name, _, setting = value.partition("=")
            values[name] = setting
    return values


def _render(tmp_path: Path, *extra: str, env: dict[str, str] | None = None) -> Path:
    out = tmp_path / "rendered"
    subprocess.run(
        ["bash", str(BUNDLE / "install.sh"), "--render-to", str(out), "--mount", MOUNT, *extra],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    return out


def _fake_getent(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """An environment whose ``getent`` answers the installer's www check
    (``--www auto``: does www.<domain> resolve like the apex?) without DNS,
    and the file that logs each lookup it was asked for."""
    bin_dir, log = tmp_path / "bin", tmp_path / "getent.log"
    bin_dir.mkdir()
    getent = bin_dir / "getent"
    getent.write_text(
        "#!/bin/sh\n"
        "# Both names at one documentation address (RFC 5737): no DNS query is made.\n"
        f"printf '%s\\n' \"$*\" >> '{log}'\n"
        "[ \"$1\" = ahostsv4 ] || exit 2\n"
        "printf '192.0.2.10      STREAM %s\\n192.0.2.10      DGRAM\\n' \"$2\"\n",
        encoding="utf-8",
    )
    getent.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', os.defpath)}"}, log


# ── create-admin ───────────────────────────────────────────────────────────


def _create_admin(argv: list[str], *, stdin: str = "") -> tuple[int, str]:
    parser_args = ["create-admin", *argv]
    output = io.StringIO()
    with (
        patch.object(cli, "_bootstrap"),
        patch("sys.stdin", io.StringIO(stdin)),
        redirect_stdout(output),
        pytest.raises(SystemExit) as stopped,
    ):
        cli.main(parser_args)
    return int(stopped.value.code or 0), output.getvalue()


@pytest.fixture()
def closed_registration(monkeypatch):
    for name in (
        "AGENT_MONITOR_REGISTRATION_OPEN",
        "AGENT_MONITOR_DATA_KEY_FILE",
        "AGENT_MONITOR_DATA_ENCRYPTION_KEY",
        "AGENT_MONITOR_SECRET_KEY",
        "AGENT_MONITOR_REQUIRE_DATA_KEY",
        "CREDENTIALS_DIRECTORY",
    ):
        monkeypatch.delenv(name, raising=False)
    assert auth.registration_open() is False


def test_create_admin_works_while_http_registration_is_closed(closed_registration):
    code, output = _create_admin(
        ["--email", "Owner@Example.com", "--password-stdin"], stdin="correct horse battery\n"
    )
    assert code == 0
    assert "correct horse battery" not in output
    user = auth.login("owner@example.com", "correct horse battery")
    assert user["is_admin"] is True and user["guest"] is False
    assert auth.registration_open() is False
    # Without a data key the public seeded defaults are stored as plaintext; a
    # console that has the key re-encrypts them on first use (see
    # test_keyless_admin_defaults_are_rewrapped_by_a_running_console), so the
    # CLI no longer demands a restart.
    assert "unencrypted" in output and "re-encrypts them" in output
    assert "no restart is needed" in output


def test_create_admin_refuses_a_second_admin_without_force(closed_registration):
    assert _create_admin(["--email", "first@example.com", "--password-stdin"], stdin="first-password\n")[0] == 0
    code, output = _create_admin(
        ["--email", "second@example.com", "--password-stdin"], stdin="second-password\n"
    )
    assert code == cli.ADMIN_EXISTS_EXIT == 3
    assert "already exists" in output
    with pytest.raises(ValueError):
        auth.login("second@example.com", "second-password")


def test_force_additional_creates_and_promotes_another_admin(closed_registration):
    assert _create_admin(["--email", "first@example.com", "--password-stdin"], stdin="first-password\n")[0] == 0
    # A pre-existing ordinary account makes register() choose non-admin.
    auth.register("member@example.com", "member-password")
    code, _output = _create_admin(
        ["--email", "second@example.com", "--password-stdin", "--force-additional"],
        stdin="second-password\n",
    )
    assert code == 0
    assert auth.login("second@example.com", "second-password")["is_admin"] is True
    assert auth.login("member@example.com", "member-password")["is_admin"] is False


def test_create_admin_ignores_guest_rows_and_non_admin_accounts(closed_registration):
    auth.create_guest_session()
    auth.register("member@example.com", "member-password")
    with sqlite3.connect(auth.DB_PATH) as conn:
        conn.execute("UPDATE users SET is_admin=0")
    code, _output = _create_admin(["--email", "owner@example.com", "--password-stdin"], stdin="owner-password\n")
    assert code == 0
    assert auth.login("owner@example.com", "owner-password")["is_admin"] is True


def test_create_admin_validates_input(closed_registration):
    code, _ = _create_admin(["--email", "owner@example.com", "--password-stdin"], stdin="short\n")
    assert code == 1
    code, _ = _create_admin(["--email", "not-an-email", "--password-stdin"], stdin="long-enough-pw\n")
    assert code == 1
    code, _ = _create_admin(["--password-stdin"], stdin="long-enough-pw\n")
    assert code == 2
    code, _ = _create_admin(
        ["--email", "owner@example.com", "--password-stdin"], stdin="first-line\nsecond-line\n"
    )
    assert code == 1
    with sqlite3.connect(auth.DB_PATH) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_interactive_password_must_be_confirmed(closed_registration):
    with patch.object(cli.getpass, "getpass", side_effect=["first-password", "other-password"]):
        code, _ = _create_admin(["--email", "owner@example.com"])
    assert code == 1
    with patch.object(cli.getpass, "getpass", side_effect=["same-password", "same-password"]):
        code, _ = _create_admin(["--email", "owner@example.com"])
    assert code == 0


def test_create_admin_encrypts_defaults_when_the_data_key_is_available(closed_registration, tmp_path, monkeypatch):
    key = tmp_path / "data.key"
    key.write_bytes(Fernet.generate_key())
    monkeypatch.setenv("AGENT_MONITOR_DATA_KEY_FILE", str(key))
    code, output = _create_admin(["--email", "owner@example.com", "--password-stdin"], stdin="owner-password\n")
    assert code == 0
    assert "unencrypted" not in output
    with sqlite3.connect(auth.DB_PATH) as conn:
        values = [row[0] for row in conn.execute("SELECT value FROM user_env")]
    assert values and all(value.startswith("enc:v1:") for value in values)


def _purge(argv: list[str]) -> tuple[int, str, str]:
    output, errors = io.StringIO(), io.StringIO()
    with (
        patch.object(cli, "_bootstrap"),
        redirect_stdout(output),
        patch("sys.stderr", errors),
        pytest.raises(SystemExit) as stopped,
    ):
        cli.main(["purge-guest-runs", *argv])
    return int(stopped.value.code or 0), output.getvalue(), errors.getvalue()


def test_purge_guest_runs_lists_by_default_and_deletes_only_with_apply(tmp_path, monkeypatch):
    from agent_monitor import jobs

    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(jobs, "_cache_dir", lambda: cache)
    monkeypatch.setattr(jobs, "_JOBS", {})
    old = "2020-01-01T00:00:00+00:00"
    for run_id, record in {
        "kimi_old_guest": {"guest": True, "status": "finished", "updated_at": old},
        "kimi_new_guest": {"guest": True, "status": "finished", "updated_at": jobs._now()},
        "kimi_old_member": {"guest": False, "status": "finished", "updated_at": old},
        "kimi_old_running": {"guest": True, "status": "running", "updated_at": old},
    }.items():
        (cache / f"{run_id}.json").write_text(json.dumps({"run_id": run_id, **record}), encoding="utf-8")
    deleted: list[str] = []
    monkeypatch.setattr(jobs, "delete_run", lambda run_id: deleted.append(run_id))

    code, output, _ = _purge([])
    assert code == 0 and deleted == []
    assert "Run data directory:" in output
    assert "1 finished guest run older than 30 days would be deleted" in output
    assert "Nothing was changed" in output and "  kimi_old_guest" in output

    code, output, _ = _purge(["--older-than-days", "7", "--apply"])
    assert code == 0 and deleted == ["kimi_old_guest"]
    assert "Deleted 1 finished guest run older than 7 days" in output
    assert "idle or stopped" in output

    for bad in ("0", "-3", "abc", "nan"):
        with pytest.raises(SystemExit) as stopped, patch.object(cli, "_bootstrap"), \
                patch("sys.stderr", io.StringIO()):
            cli.main(["purge-guest-runs", "--older-than-days", bad, "--apply"])
        assert stopped.value.code == 2
    assert deleted == ["kimi_old_guest"]


def test_create_admin_uses_the_configured_data_directory(tmp_path):
    data_dir = tmp_path / "console-data"
    env = {
        **os.environ,
        "AGENT_MONITOR_DATA_DIR": str(data_dir),
        "AGENT_MONITOR_ENV_PATH": str(tmp_path / "missing.env"),
    }
    for name in ("AGENT_MONITOR_REGISTRATION_OPEN", "AGENT_MONITOR_REQUIRE_DATA_KEY"):
        env.pop(name, None)
    completed = subprocess.run(
        [sys.executable, "-m", "agent_monitor.cli", "create-admin", "--email", "owner@example.com", "--password-stdin"],
        input="subprocess-password\n",
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert str(data_dir / "users.db") in completed.stdout
    assert "subprocess-password" not in completed.stdout + completed.stderr
    with sqlite3.connect(data_dir / "users.db") as conn:
        rows = conn.execute("SELECT email, is_admin, is_guest FROM users").fetchall()
    assert rows == [("owner@example.com", 1, 0)]
    assert not (ROOT / "data" / "users.db").exists() or "owner@example.com" not in (
        ROOT / "data" / "users.db"
    ).read_bytes().decode("latin-1")


# ── Caddyfile ──────────────────────────────────────────────────────────────


def test_caddyfile_routes_sign_in_before_the_static_site_and_hides_the_broker():
    caddy = (BUNDLE / "Caddyfile").read_text(encoding="utf-8")
    site = caddy[caddy.index("\nansatze.ai {") :]
    signin = site.index("handle @signin")
    assert signin < site.index("handle @console") < site.index("\thandle {")
    for variant in ("/sign-in ", "/sign-in/ ", "/sign-in.html ", "/docs/sign-in ", "/docs/sign-in/ "):
        assert variant in site
    # Must land on <mount>/login (docs researchApi.js contract), temporarily.
    assert "redir * @@MOUNT@@/login 302" in site
    assert "redir @signin_query @@MOUNT@@/login?{query} 302" in site
    directives = re.sub(r"(?m)^\s*#.*$", "", caddy)
    assert "{?query}" not in directives  # unsupported by the caddy 2.6 package
    assert "@console path @@MOUNT@@ @@MOUNT@@/*" in site
    assert site.count("reverse_proxy") == 1 and "reverse_proxy 127.0.0.1:4600" in site
    assert "4610" not in re.sub(r"(?m)^\s*#.*$", "", caddy)
    assert re.search(r"@internal path /internal /internal/\* @@MOUNT@@/internal", site)
    assert site.index("handle @internal") < site.index("handle @console")
    assert "max_size 30MiB" in site  # >= the console's 29 MiB request ceiling
    # Edge headers live in one snippet that every site imports, and that
    # handle_errors imports again: error routes (the docs 404 page) are a
    # separate route list the site-level header directive does not reach.
    snippet = caddy[caddy.index("(edge_headers) {") : caddy.index("#@www-begin")]
    assert 'Strict-Transport-Security "max-age=31536000"' in snippet
    for directive in ("-Server", '?X-Content-Type-Options "nosniff"', '?X-Frame-Options "SAMEORIGIN"', "defer"):
        assert directive in snippet
    assert caddy.index("(edge_headers) {") < caddy.index("\nansatze.ai {")
    assert site.count("import edge_headers") == 2
    errors = site[site.index("handle_errors {") :]
    assert errors.index("import edge_headers") < errors.index("rewrite * /404.html")
    www = caddy[caddy.index("www.ansatze.ai {") : caddy.index("#@www-end")]
    assert "import edge_headers" in www
    assert "try_files {path} {path}.html {path}/index.html" in site
    assert "rewrite * /404.html" in site
    assert "redir https://ansatze.ai{uri} 308" in caddy
    assert "/var/lib/ansatze-news" in site
    # The live problem-status overlay mirrors the news route and falls back to
    # the bundled file; catalogue data is revalidated, never heuristically cached.
    live = site[site.index("@liveproblemstatus {") : site.index("handle @liveproblemstatus")]
    assert "path /research/problem-status.json" in live and "root /var/lib/ansatze-problems" in live
    assert "try_files /problem-status.json" in live
    handler = site[site.index("handle @liveproblemstatus {") :]
    handler = handler[: handler.index("\n\t}")]
    assert 'header Cache-Control "no-cache"' in handler and "rewrite * /problem-status.json" in handler
    assert site.index("handle @liveproblemstatus") < site.index("\thandle {")
    docs = site[site.index("\thandle {") :]
    assert "@problemdata path /research/open-problems.json /open-problems.json /research/problem-status.json" in docs
    assert 'header @problemdata Cache-Control "no-cache"' in docs


def test_rendered_caddyfile_substitutes_every_token(tmp_path):
    out = _render(tmp_path, "--www", "yes", "--acme-email", "owner@example.com")
    caddy = (out / "Caddyfile").read_text(encoding="utf-8")
    assert "@@" not in caddy
    assert f"redir * {MOUNT}/login 302" in caddy
    assert "www.ansatze.ai {" in caddy and "email owner@example.com" in caddy

    minimal = _render(tmp_path / "minimal", "--www", "no")
    text = (minimal / "Caddyfile").read_text(encoding="utf-8")
    assert "www.ansatze.ai {" not in text and "email " not in text
    assert text.count("{") == text.count("}")


def test_rendered_caddyfile_parses_with_caddy_when_available(tmp_path):
    caddy = os.environ.get("CADDY_BIN") or shutil.which("caddy")
    if not caddy:
        pytest.skip("caddy binary not available (set CADDY_BIN to check)")
    out = _render(tmp_path, "--www", "yes", "--acme-email", "owner@example.com")
    completed = subprocess.run(
        [caddy, "adapt", "--config", str(out / "Caddyfile"), "--adapter", "caddyfile"],
        capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    config = json.loads(completed.stdout)
    routes = json.dumps(config)
    assert f"{MOUNT}/login" in routes and "127.0.0.1:4600" in routes


# ── systemd units ──────────────────────────────────────────────────────────


def test_console_unit_holds_no_kimi_key_and_keeps_registration_closed():
    unit = _unit("agent-monitor.service")
    env = _environment(unit)
    directives = dict(_directives(unit))
    assert directives["User"] == "ubuntu"
    assert directives["WorkingDirectory"] == "/home/repo/agent_monitor_math"
    assert "ProtectHome" not in directives  # app and CLIs live in /home
    assert directives["NoNewPrivileges"] == "yes"
    assert env["PYTHONPATH"] == "/home/repo/agent_monitor_math"
    assert env["AGENT_MONITOR_DATA_DIR"] == "/var/lib/ansatze/console"
    assert env["AGENT_MONITOR_ENV_PATH"] == "/etc/ansatze/console.env"
    assert env["AGENT_MONITOR_HOST"] == "127.0.0.1"
    assert env["AGENT_MONITOR_PORT"] == "4600"
    assert env["AGENT_MONITOR_BASE_PATH"] == "@@MOUNT@@"
    assert env["AGENT_MONITOR_APP_URL"] == "https://ansatze.ai"
    assert env["AGENT_MONITOR_PUBLIC"] == "0"
    assert env["AGENT_MONITOR_GUEST_ACCESS"] == "1"
    assert env["AGENT_MONITOR_SPONSORED_KIMI"] == "1"
    assert env["AGENT_MONITOR_SPONSORED_KIMI_URL"] == "http://127.0.0.1:4610"
    assert env["AGENT_MONITOR_REQUIRE_DATA_KEY"] == "1"
    assert "LoadCredential=agent_monitor_data_key:/etc/ansatze/credentials/agent_monitor_data_key" in unit
    for directory in (
        "/home/ubuntu/.local/bin",
        "/home/ubuntu/.elan/bin",
        "/home/ubuntu/.local/node24/bin",
        "/home/ubuntu/.npm-global/bin",
    ):
        assert directory in env["PATH"].split(":")
    assert "AGENT_MONITOR_REGISTRATION_OPEN" not in env
    assert not [name for name in env if "KIMI_API" in name or name.endswith("_API_KEY")]
    assert "kimi_api_key" not in unit
    assert "AGENT_MONITOR_MATHLIB_PROJECT" not in env
    # Large or stalled uploads and runaway harnesses hit unit ceilings, not
    # the host (GI-3); finished guest runs are purged in-process (GI-4).
    assert directives["MemoryMax"] == "12G" and directives["MemoryHigh"] == "10G"
    assert directives["TasksMax"] == "4096"
    assert env["AGENT_MONITOR_GUEST_RUN_RETENTION_DAYS"] == "30"


def test_kimi_broker_unit_is_the_only_key_holder_and_is_loopback_only():
    unit = _unit("ansatze-kimi.service")
    env = _environment(unit)
    directives = _directives(unit)
    single = dict(directives)
    assert single["User"] == "ansatze-kimi" and single["User"] != "ubuntu"
    assert env["PUBLIC_KIMI_DATA_DIR"] == env["AGENT_MONITOR_DATA_DIR"] == "/var/lib/ansatze/kimi"
    assert env["PUBLIC_KIMI_HOST"] == "127.0.0.1"
    assert env["PUBLIC_KIMI_PORT"] == "4610"
    assert env["AGENT_MONITOR_ENV_PATH"] == "/dev/null"
    assert "LoadCredential=kimi_api_key:/etc/ansatze/credentials/kimi_api_key" in unit
    assert "LoadCredential=kimi_api_base:/etc/ansatze/credentials/kimi_api_base" in unit
    assert "KIMI_API_KEY" not in env and "PUBLIC_KIMI_ENV_PATH" not in env
    assert single["ProtectHome"] == "tmpfs"
    assert ("BindReadOnlyPaths", "/home/repo/agent_monitor_math") in directives
    assert single["ProtectSystem"] == "strict"
    assert single["NoNewPrivileges"] == "yes"
    assert single["CapabilityBoundingSet"] == ""
    assert "/home/repo/agent_monitor_math/.secrets" in single["InaccessiblePaths"]
    assert "/var/lib/ansatze/console" in single["InaccessiblePaths"]


def test_console_cannot_see_the_broker_state_or_credentials():
    unit = _unit("agent-monitor.service")
    inaccessible = dict(_directives(unit))["InaccessiblePaths"]
    assert "/var/lib/ansatze/kimi" in inaccessible
    assert "/etc/ansatze/credentials" in inaccessible


def test_news_units_publish_only_through_the_broker():
    service = _unit("ansatze-news.service")
    env = _environment(service)
    assert env["AGENT_MONITOR_SPONSORED_KIMI_URL"] == "http://127.0.0.1:4610"
    assert "LoadCredential" not in service and "KIMI_API_KEY" not in env
    assert "--output /var/lib/ansatze-news/news.json" in service
    assert "OnCalendar=" in _unit("ansatze-news.timer")


def test_problem_status_units_publish_only_through_the_broker():
    service = _unit("ansatze-problems.service")
    env = _environment(service)
    directives = dict(_directives(service))
    assert env["AGENT_MONITOR_SPONSORED_KIMI_URL"] == "http://127.0.0.1:4610"
    assert "LoadCredential" not in service and not [name for name in env if "KEY" in name]
    assert directives["User"] == directives["Group"] == "ansatze-problems"
    assert directives["StateDirectory"] == "ansatze-problems"
    assert directives["StateDirectoryMode"] == "0755" and directives["UMask"] == "0022"
    assert directives["MemoryMax"] == "768M"
    assert "--output /var/lib/ansatze-problems/problem-status.json" in directives["ExecStart"]
    assert "--state /var/lib/ansatze-problems/state.json" in directives["ExecStart"]
    assert "scripts/update_problem_status.py" in directives["ExecStart"]
    # Same sandbox as the news worker: only names, paths, limits and the command differ.
    news = dict(_directives(_unit("ansatze-news.service")))
    differing = {key for key in set(news) | set(directives) if news.get(key) != directives.get(key)}
    assert differing <= {"Description", "User", "Group", "StateDirectory", "WorkingDirectory", "ExecStart",
                         "Environment", "TimeoutStartSec", "MemoryMax"}
    sandbox = [line for line in _directives(service) if line[0] in ("InaccessiblePaths", "BindReadOnlyPaths", "SystemCallFilter")]
    assert sandbox == [line for line in _directives(_unit("ansatze-news.service")) if line[0] in ("InaccessiblePaths", "BindReadOnlyPaths", "SystemCallFilter")]
    timer = dict(_directives(_unit("ansatze-problems.timer")))
    assert timer["OnCalendar"] == "*-*-* 00/6:15:00"
    assert timer["RandomizedDelaySec"] == "300" and timer["Persistent"] == "true"
    installer = (BUNDLE / "install.sh").read_text(encoding="utf-8")
    assert "--with-problem-status) WITH_PROBLEMS=1" in installer
    assert "run systemctl enable --now ansatze-problems.timer" in installer
    assert '"${PROBLEMS_USER}"' in installer and "PROBLEMS_STATE=/var/lib/ansatze-problems" in installer


def test_rendered_units_pass_systemd_analyze_verify(tmp_path):
    analyzer = shutil.which("systemd-analyze")
    if not analyzer:
        pytest.skip("systemd-analyze not available")
    # The default --www auto looks both names up: here a fake getent answers (the
    # suite never queries DNS), so www resolves like the apex and its redirect is kept.
    env, lookups = _fake_getent(tmp_path)
    out = _render(tmp_path, env=env)
    assert lookups.read_text(encoding="utf-8").splitlines() == ["ahostsv4 ansatze.ai", "ahostsv4 www.ansatze.ai"]
    assert "www.ansatze.ai {" in (out / "Caddyfile").read_text(encoding="utf-8")
    assert "@@" not in (out / "agent-monitor.service").read_text(encoding="utf-8")
    assert f"AGENT_MONITOR_BASE_PATH={MOUNT}" in (out / "agent-monitor.service").read_text(encoding="utf-8")
    broker = out / "ansatze-kimi.service"
    assert dict(_directives(broker.read_text(encoding="utf-8")))["PrivatePIDs"] == "yes"
    version = subprocess.run([analyzer, "--version"], capture_output=True, text=True, check=True, timeout=10)
    major = int(re.match(r"systemd\s+(\d+)", version.stdout).group(1))
    if major < 257:
        # PrivatePIDs arrived in systemd 257. Validate all other directives on
        # older development/CI hosts; the production template above retains
        # PID isolation, and supported hosts verify the unmodified unit.
        broker.write_text(re.sub(r"(?m)^PrivatePIDs=yes\n", "", broker.read_text(encoding="utf-8")), encoding="utf-8")
    # verify also checks that ExecStart's executable exists. CI has no
    # production checkout, so use its real interpreter in these temporary
    # verification copies while preserving every argument and other directive.
    for name in UNITS:
        unit = out / name
        source = unit.read_text(encoding="utf-8")
        if name.endswith(".service"):
            production_command = "ExecStart=/home/repo/agent_monitor_math/.venv/bin/python "
            assert source.count(production_command) == 1
            unit.write_text(source.replace(production_command, f"ExecStart={Path(sys.executable).resolve()} "), encoding="utf-8")
    completed = subprocess.run(
        [analyzer, "verify", "--man=no", *[f"./{name}" for name in UNITS]],
        cwd=out, capture_output=True, text=True, timeout=120,
    )
    own = [line for line in completed.stderr.splitlines() if str(out) in line or line.startswith("./")]
    assert completed.returncode == 0 and not own, completed.stderr


def test_legacy_top_level_deploy_files_point_to_the_bundle():
    for legacy in ("agent-monitor.service", "Caddyfile"):
        text = (ROOT / "deploy" / legacy).read_text(encoding="utf-8")
        assert "deploy/ansatze" in text
        assert "sslip.io" not in text and "proof-cb44" not in text
        assert not [line for line in text.splitlines() if line.strip() and not line.startswith("#")]


# ── installer and smoke scripts ────────────────────────────────────────────


def test_scripts_are_valid_bash():
    for script in ("install.sh", "smoke.sh"):
        path = BUNDLE / script
        assert os.access(path, os.X_OK), f"{script} must be executable"
        subprocess.run(["bash", "-n", str(path)], check=True, timeout=30)


def test_installer_keeps_secrets_off_command_lines_and_out_of_the_console_env():
    installer = (BUNDLE / "install.sh").read_text(encoding="utf-8")
    assert "printf '%s' \"${value}\" >\"${tmp}\"" in installer  # builtin write
    assert not re.search(r"echo[^\n]*STAGED_KIMI_KEY", installer)
    assert "run install -m 0400 -o root -g root" in installer
    assert "--update-kimi-key" in installer and "--dry-run" in installer
    assert "AGENT_MONITOR_REGISTRATION_OPEN" not in installer
    # The generated console.env documents SMTP/Google only and rejects Kimi keys.
    console_env = installer[installer.index("cat >\"${tmp}\" <<'EOF'") :]
    console_env = console_env[: console_env.index("\nEOF\n")]
    assert not [line for line in console_env.splitlines()[1:] if line and not line.startswith("#")]
    assert "contains a Kimi key" in installer
    # The data key is never regenerated once it exists.
    assert "keeping existing ${CRED_DIR}/agent_monitor_data_key" in installer
    # Lean pins and the measure-then-refuse gate.
    assert "defeb1c29ed4009c5529b5acb06917a04923f53beb1098c28164b9c8cebc0c84" in installer
    assert "0840a02e6c2cf888721fa183eb49a397b188d5d24c6598e98534f2b3190a297e" in installer
    assert "--measure-only" in installer and "--accept-measured" in installer
    steps = re.search(r"ALL_STEPS=\(([^)]*)\)", installer).group(1).split()
    assert steps == ["packages", "credentials", "render", "services", "lean", "docs", "caddy", "admin", "smoke"]


def test_installer_parses_the_staged_kimi_file_without_printing_it(tmp_path):
    staged = tmp_path / "kimi.env"
    secret = "fixture-" + "k" * 30
    staged.write_text(
        f'# staged\r\nexport KIMI_API_KEY="{secret}"\r\nKIMI_API_BASE = https://kimi.example/v1\nOTHER=1\n',
        encoding="utf-8",
    )
    # Named outside the f-string: reusing the quote character inside a
    # replacement field is Python 3.12 syntax, and 3.11 is supported.
    install_script = BUNDLE / "install.sh"
    script = (
        "set -euo pipefail\n"
        f"APP_DIR={ROOT}\n"
        'die() { echo "die: $*"; exit 1; }\n'
        f'eval "$(sed -n \'/^read_staged_kimi() {{/,/^}}/p\' {install_script})"\n'
        'read_staged_kimi "$1"\n'
        'printf "%s %s\\n" "${#STAGED_KIMI_KEY}" "${STAGED_KIMI_BASE}"\n'
        '[[ ${STAGED_KIMI_KEY} == "$2" ]] && echo same\n'
    )
    completed = subprocess.run(
        ["bash", "-c", script, "parse", str(staged), secret],
        capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.split() == [str(len(secret)), "https://kimi.example/v1", "same"]
    staged.write_text("KIMI_API_BASE=http://plain.example/v1\nKIMI_API_KEY=short\n", encoding="utf-8")
    rejected = subprocess.run(
        ["bash", "-c", script, "parse", str(staged), "short"], capture_output=True, text=True, timeout=30
    )
    assert rejected.returncode != 0 and "no usable KIMI_API_KEY" in rejected.stdout


def test_test_suite_never_uses_the_checkout_state():
    from agent_monitor import DATA_DIR, settings

    assert not DATA_DIR.is_relative_to(ROOT)
    assert not auth.DB_PATH.is_relative_to(ROOT)
    assert settings.ENV_PATH != ROOT / ".env"
    assert not settings.SYSTEM_SETTINGS_PATH.is_relative_to(ROOT)


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") and ROOT == Path("/home/repo/agent_monitor_math")
         and (ROOT / ".venv" / "bin" / "python").exists()
         and os.environ.get("USER", "") in {"ubuntu", "root"}),
    reason="dry run needs the production checkout layout and service account",
)
def test_installer_dry_run_changes_nothing_and_never_prints_the_key(tmp_path):
    fake_key = "sk-fake-" + "q" * 40
    staged = tmp_path / "kimi.env"
    staged.write_text(f"KIMI_API_KEY={fake_key}\nKIMI_API_BASE=https://kimi.example/v1\n", encoding="utf-8")
    staged.chmod(0o600)
    mount_file = Path("/etc/ansatze/mount")

    def _mount_state():
        try:
            info = mount_file.stat()
        except FileNotFoundError:
            return None
        return (info.st_ino, info.st_mtime_ns, info.st_size)

    mount_before = _mount_state()
    completed = subprocess.run(
        ["bash", str(BUNDLE / "install.sh"), "--dry-run", "--kimi-env", str(staged), "--skip", "lean"],
        capture_output=True, text=True, timeout=300,
        env={**os.environ, "TMPDIR": str(tmp_path)},
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert fake_key not in output
    assert "DRY RUN" in output
    # On a fresh host the units are (re)started; on a deployed host with
    # unchanged units the idempotent installer reports them as running.
    kimi_markers = ("systemctl restart ansatze-kimi.service", "ansatze-kimi.service is unchanged and running")
    console_markers = ("systemctl restart agent-monitor.service", "agent-monitor.service is unchanged and running")
    kimi_at = min(output.index(m) for m in kimi_markers if m in output)
    console_at = min(output.index(m) for m in console_markers if m in output)
    assert kimi_at < console_at
    # A dry run never creates or rewrites the persisted mount.
    assert _mount_state() == mount_before


def test_smoke_script_requires_a_valid_mount(tmp_path):
    bad = subprocess.run(
        ["bash", str(BUNDLE / "smoke.sh"), "--mount", "/proof-nothex", "--no-edge"],
        capture_output=True, text=True, timeout=30,
    )
    assert bad.returncode == 2
    helped = subprocess.run(["bash", str(BUNDLE / "smoke.sh"), "--help"], capture_output=True, text=True, timeout=30)
    assert helped.returncode == 0 and "--guest-run" in helped.stdout


def test_bundle_readme_covers_prerequisites_and_owner_actions():
    readme = (BUNDLE / "README.md").read_text(encoding="utf-8")
    for phrase in (
        "security group",
        "www.ansatze.ai",
        "Cloudflare",
        "sudo deploy/ansatze/install.sh",
        "create-admin",
        "Codex",
        "Claude Code",
        "Rotate the Kimi key",
        "spend cap",
        "AGENT_MONITOR_REGISTRATION_OPEN",
        "--uninstall",
        "smoke.sh",
    ):
        assert phrase in readme, phrase


# ── docs publishing ───────────────────────────────────────────────────────


def test_docs_publish_falls_back_when_mv_exchange_is_unavailable(tmp_path):
    node = shutil.which("node") or "/home/ubuntu/.local/node24/bin/node"
    if not Path(node).exists():
        pytest.skip("node is not available")
    script = ROOT / "scripts" / "build_docs_atomic.mjs"
    harness = tmp_path / "harness.mjs"
    harness.write_text(
        "import { mkdirSync, writeFileSync, readFileSync } from 'node:fs'\n"
        "import { join } from 'node:path'\n"
        f"import {{ publish }} from {json.dumps(script.as_uri())}\n"
        "const results = {}\n"
        "for (const method of ['renameat2', 'rename']) {\n"
        "  const dir = join(process.argv[2], method); mkdirSync(dir)\n"
        "  const pub = join(dir, 'dist'), stage = join(dir, 'stage')\n"
        "  mkdirSync(pub); writeFileSync(join(pub, 'v'), 'old')\n"
        "  mkdirSync(stage); writeFileSync(join(stage, 'v'), 'new')\n"
        "  const used = publish(stage, pub, { method })\n"
        "  results[method] = [used, readFileSync(join(pub, 'v'), 'utf8'), readFileSync(join(stage, 'v'), 'utf8')]\n"
        "}\n"
        "console.log(JSON.stringify(results))\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [node, str(harness), str(tmp_path)], capture_output=True, text=True, timeout=60
    )
    assert completed.returncode == 0, completed.stderr
    results = json.loads(completed.stdout.strip().splitlines()[-1])
    # The new release is published and the old one is left for cleanup.
    assert results["rename"] == ["rename", "new", "old"]
    if sys.platform.startswith("linux") and shutil.which("python3"):
        assert results["renameat2"] == ["renameat2", "new", "old"]


def test_docs_sitemap_uses_the_canonical_origin():
    config = (ROOT / "docs" / ".vitepress" / "config.mjs").read_text(encoding="utf-8")
    assert "hostname: 'https://ansatze.ai'" in config


def test_retired_host_scripts_refuse_to_touch_the_new_host():
    for name in ("activate_public_signup.py", "enable_research_compression.py"):
        path = ROOT / "scripts" / name
        text = path.read_text(encoding="utf-8")
        assert "sslip.io" not in text and "proof-cb44" not in text
        # No privileged side effects remain: nothing is executed or written.
        assert "subprocess" not in text and "open(" not in text and "write" not in text
        completed = subprocess.run(
            [sys.executable, str(path), "--apply"], capture_output=True, text=True, timeout=30
        )
        assert completed.returncode != 0
        assert "deploy/ansatze" in completed.stdout + completed.stderr
