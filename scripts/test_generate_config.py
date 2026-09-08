"""Tests for the manifest loader and config generator.

Run from the repo root: `python -m unittest scripts/test_generate_config.py`.
They exercise the routing contract the gateway promises - Host routing per project
(including extra hosts), path-prefix routing with the prefix stripped, the
Referer-routed /openapi.json helper, the health route, the explicit 404, port
assignment, and the duplicate-claim guards - against a manifest written to a temp
dir, never the live one.
"""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ.setdefault("GATEWAY_APP_ROOT", "/app")
import manifest as m  # noqa: E402
import generate_config as g  # noqa: E402

MANIFEST = textwrap.dedent(
    """
    [gateway]
    base_domain = "example.com"
    service_name = "My Gateway"
    port_base = 9001

    [[project]]
    name = "alpha"
    path = "projects/alpha"
    backend_dir = "fastapi_backend"
    app = "main:app"
    extra_hosts = ["api.alpha.io"]
    db = "external"

    [[project]]
    name = "beta"
    path = "projects/beta"
    backend_dir = "."
    app = "fastapi_backend.main:app"
    requirements = "fastapi_backend/requirements.txt"
    path_prefixes = ["/beta", "/old-beta"]
    health_path = "/"

    [[project]]
    name = "gamma"
    path = "projects/gamma"
    path_prefixes = []
    port = 9100

    [[project]]
    name = "off"
    path = "projects/off"
    enabled = false
    """
)


def _load(text: str) -> m.GatewayConfig:
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
        fh.write(text)
        path = fh.name
    try:
        return m.load(path)
    finally:
        os.unlink(path)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.cfg = _load(MANIFEST)
        self.by_name = {p.name: p for p in self.cfg.projects}

    def test_disabled_projects_are_dropped(self):
        self.assertEqual([p.name for p in self.cfg.projects], ["alpha", "beta", "gamma"])

    def test_service_name_and_hosts(self):
        self.assertEqual(self.cfg.service_name, "My Gateway")
        self.assertEqual(self.cfg.host_template, "api.{name}.{base_domain}")
        alpha = self.by_name["alpha"]
        self.assertEqual(alpha.hostname, "api.alpha.example.com")
        self.assertEqual(alpha.hosts, ("api.alpha.example.com", "api.alpha.io"))

    def test_host_template_override(self):
        cfg = _load(MANIFEST.replace('port_base = 9001', 'port_base = 9001\nhost_template = "{subdomain}.api.{base_domain}"'))
        self.assertEqual({p.name: p.hostname for p in cfg.projects},
                         {"alpha": "alpha.api.example.com", "beta": "beta.api.example.com", "gamma": "gamma.api.example.com"})

    def test_ports_are_sequential_with_explicit_override(self):
        self.assertEqual(self.by_name["alpha"].port, 9001)
        self.assertEqual(self.by_name["beta"].port, 9002)
        self.assertEqual(self.by_name["gamma"].port, 9100)

    def test_paths_resolve_against_app_root(self):
        alpha, beta = self.by_name["alpha"], self.by_name["beta"]
        self.assertEqual(alpha.backend_dir, "/app/projects/alpha/fastapi_backend")
        self.assertEqual(alpha.requirements, "/app/projects/alpha/fastapi_backend/requirements.txt")
        self.assertEqual(beta.backend_dir, "/app/projects/beta")
        self.assertEqual(beta.requirements, "/app/projects/beta/fastapi_backend/requirements.txt")
        self.assertEqual(beta.venv, "/app/projects/beta/.venv")

    def test_path_prefix_defaults_and_opt_out(self):
        self.assertEqual(self.by_name["alpha"].path_prefixes, ("/alpha",))
        self.assertEqual(self.by_name["beta"].path_prefixes, ("/beta", "/old-beta"))
        self.assertEqual(self.by_name["gamma"].path_prefixes, ())

    def test_duplicate_host_is_rejected(self):
        bad = MANIFEST.replace('path_prefixes = ["/beta", "/old-beta"]',
                               'path_prefixes = ["/beta"]\nextra_hosts = ["api.alpha.io"]')
        with self.assertRaises(ValueError):
            _load(bad)

    def test_duplicate_prefix_is_rejected(self):
        bad = MANIFEST.replace('path_prefixes = ["/beta", "/old-beta"]', 'path_prefixes = ["/beta", "/alpha"]')
        with self.assertRaises(ValueError):
            _load(bad)

    def test_bad_prefix_is_rejected(self):
        bad = MANIFEST.replace('path_prefixes = ["/beta", "/old-beta"]', 'path_prefixes = ["/beta/*"]')
        with self.assertRaises(ValueError):
            _load(bad)


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.cfg = _load(MANIFEST)
        self.files = g.render(self.cfg)
        self.caddy = self.files["Caddyfile"]

    def test_every_project_has_a_host_block(self):
        for host, port in (
            ("http://api.alpha.example.com:{$PORT:10000}, http://api.alpha.io:{$PORT:10000}", 9001),
            ("http://api.beta.example.com:{$PORT:10000}", 9002),
            ("http://api.gamma.example.com:{$PORT:10000}", 9100),
        ):
            self.assertIn(f"{host} {{\n", self.caddy)
            self.assertIn(f"\thandle {{\n\t\treverse_proxy 127.0.0.1:{port}\n\t}}", self.caddy)

    def test_health_answers_on_every_host_not_just_the_catch_all(self):
        # Render's health check carries a Host header of its own choosing; when that is
        # a project's hostname, a health route only in the catch-all lets the request
        # reach the app, which 404s and fails the deploy (measured 2026-09-08).
        blocks = self.caddy.split("\n}\n")
        routed = [b for b in blocks if "reverse_proxy" in b or "gateway: unknown host" in b]
        self.assertEqual(len(routed), 4)  # three projects + the catch-all
        for b in routed:
            self.assertIn("@health path /__gateway/health", b)

    def test_host_blocks_precede_the_catch_all(self):
        self.assertLess(self.caddy.index("http://api.alpha.example.com"), self.caddy.index("http://:{$PORT:10000} {"))

    def test_path_prefixes_are_stripped_and_redirected(self):
        for prefix, port in (("/alpha", 9001), ("/beta", 9002), ("/old-beta", 9002)):
            self.assertIn(f"handle_path {prefix}/* {{\n\t\treverse_proxy 127.0.0.1:{port}", self.caddy)
            self.assertIn(f"path {prefix}\n", self.caddy)
            self.assertIn(f" {prefix}/ 308", self.caddy)
        self.assertNotIn("handle_path /gamma/*", self.caddy)

    def test_openapi_is_routed_by_referer(self):
        self.assertIn("header_regexp Referer ^https?://[^/]+/old\\-beta(/|$)", self.caddy)
        self.assertIn("header_regexp Referer ^https?://[^/]+/alpha(/|$)", self.caddy)

    def test_health_and_404_and_trusted_proxies(self):
        self.assertIn("@health path /__gateway/health", self.caddy)
        self.assertIn('respond "gateway: unknown host" 404', self.caddy)
        self.assertIn("trusted_proxies static 0.0.0.0/0 ::/0", self.caddy)
        self.assertIn("auto_https off", self.caddy)
        # The 404 fallback is the last handle in the file.
        self.assertGreater(self.caddy.rindex("respond \"gateway: unknown host\""), self.caddy.rindex("handle_path"))

    def test_supervisord_has_one_program_per_project_plus_caddy(self):
        sup = self.files["supervisord.conf"]
        for name in ("caddy", "alpha", "beta", "gamma"):
            self.assertIn(f"[program:{name}]", sup)
        self.assertNotIn("[program:off]", sup)
        self.assertIn("command=/app/scripts/launch.sh alpha", sup)

    def test_runspec_carries_no_secrets_only_paths(self):
        spec = self.files["run.d/beta.env"]
        self.assertIn("PROJECT_APP=fastapi_backend.main:app", spec)
        self.assertIn("PROJECT_DIR=/app/projects/beta", spec)
        self.assertIn("PROJECT_PORT=9002", spec)
        self.assertIn("PROJECT_DB=sqlite", spec)
        self.assertIn("PROJECT_DB_PATH=/data/beta.db", spec)
        self.assertNotIn("DATABASE_URL", spec)


if __name__ == "__main__":
    unittest.main()
