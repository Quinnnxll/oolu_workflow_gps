"""The production deploy config: mounts that survive a git deploy.

Exit gate (the stale-investor-panel fix): the caddy container mounts
the deploy directory WHOLE — a single-file bind mount pins the file's
inode at container start, and every ``git reset --hard`` on deploy
replaces changed files with NEW inodes, so the container would keep
serving the page and config as they were the day it was created while
every deploy "succeeded". These pins keep the compose file, the
Caddyfile, and the workflow agreeing on the one mounted path.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
COMPOSE = (_ROOT / "docker-compose.prod.yml").read_text()
CADDYFILE = (_ROOT / "deploy" / "Caddyfile").read_text()
WORKFLOW = (_ROOT / ".github" / "workflows" / "deploy.yml").read_text()


def test_the_deploy_directory_is_mounted_whole_never_file_by_file():
    assert "./deploy:/srv/deploy:ro" in COMPOSE
    # No single-file bind mount of deploy content may return: it is
    # exactly the shape that served a stale panel for weeks.
    assert "./deploy/" not in COMPOSE
    # Caddy runs from the mounted config, not the image's default path.
    assert "caddy run --config /srv/deploy/Caddyfile" in COMPOSE


def test_the_three_files_agree_on_the_mounted_path():
    assert "root * /srv/deploy" in CADDYFILE
    assert "rewrite * /investor-panel.html" in CADDYFILE
    # Browsers REVALIDATE (ETag keeps it a cheap 304): a deployed
    # panel is the panel people see.
    assert 'header Cache-Control "no-cache"' in CADDYFILE
    # The workflow validates and reloads the SAME file Caddy runs from.
    assert WORKFLOW.count("--config /srv/deploy/Caddyfile") == 2
    assert "/etc/caddy/Caddyfile" not in WORKFLOW


def test_the_panel_in_the_repo_is_the_live_wired_version():
    panel = (_ROOT / "deploy" / "investor-panel.html").read_text()
    # Same-origin by default; the token paste survives only as the
    # advanced fallback — never the front door.
    assert "empty = this origin" in panel
    assert "signin-form" in panel
