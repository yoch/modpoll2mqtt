# Configuration file for the Sphinx documentation builder.

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath("../"))

project = "modpoll2mqtt"
copyright = "2021-2026 Ying Shaodong; 2026 yoch"
author = "yoch"

_pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
_match = re.search(
    r'^version = "([^"]+)"', _pyproject.read_text(encoding="utf-8"), re.M
)
version = release = _match.group(1) if _match else "0.0.0"

extensions = [
    "sphinx.ext.githubpages",
    "sphinxarg.ext",
    "sphinx_sitemap",
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

html_theme = "sphinx_rtd_theme"
html_static_path = ["assets"]
html_title = "modpoll2mqtt – Modbus (RTU/TCP/UDP) to MQTT gateway"

# Canonical URL of the published site: emits <link rel="canonical"> and is the
# base for sitemap.xml (sphinx-sitemap).
html_baseurl = "https://yoch.github.io/modpoll2mqtt/"
sitemap_url_scheme = "{link}"
sitemap_excludes = ["search.html", "genindex.html"]

# "Edit on GitHub" link in the sphinx_rtd_theme header.
html_context = {
    "display_github": True,
    "github_user": "yoch",
    "github_repo": "modpoll2mqtt",
    "github_version": "main",
    "conf_py_path": "/docs/",
}
