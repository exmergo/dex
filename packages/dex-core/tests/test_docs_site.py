"""The documentation site build: `scripts/build_docs_site.py`.

The site is generated from `docs/`, `references/`, the CLI parser and
`CHANGELOG.md` on every release, and nobody reads the generated branch before it
goes live. So everything the build promises (every reference published, every
link resolving, nothing MDX would choke on, nothing private leaking) is held
here, against the real repository and against small synthetic ones.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "build_docs_site.py"

pytestmark = pytest.mark.skipif(
    not (
        SCRIPT.is_file() and (ROOT / "references").is_dir() and (ROOT / "docs").is_dir()
    ),
    reason="needs a repository checkout with the docs site sources",
)


def _load():
    spec = importlib.util.spec_from_file_location("build_docs_site", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: a dataclass resolves its own module by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


site = _load() if SCRIPT.is_file() else None

DESC = ("A synthetic page description used by the documentation site tests. " * 3)[:150]


# --- Synthetic repositories -------------------------------------------------


def _parser() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--repo-root", default=argparse.SUPPRESS, help="where to run")
    parser = argparse.ArgumentParser(
        prog="dex", description="A tool.", epilog="Run `dex tool run` first."
    )
    parser.add_argument("--repo-root", default=".", help="where to run")
    parser.add_argument("--engine", help="how to build the engine")
    groups = parser.add_subparsers(dest="group")
    tool = groups.add_parser("tool", parents=[shared], help="the tool group")
    subs = tool.add_subparsers(dest="subcommand")
    run = subs.add_parser(
        "run", parents=[shared], help="run it", description="Runs it. Free."
    )
    run.add_argument("objects", nargs="*", metavar="object", help="what to run")
    run.add_argument("--mode", choices=["a", "b"], help="which mode")
    run.add_argument("--tag", action="append", help="a tag; repeatable")
    run.add_argument("--quiet", action="store_true", help="say less")
    run.add_argument("--hidden", default=argparse.SUPPRESS, help="kept out of <name>/")
    pick = run.add_mutually_exclusive_group()
    pick.add_argument("--yes", action="store_true", help="agree")
    pick.add_argument("--no", action="store_true", help="disagree")
    return parser, shared


def make_repo(
    root: Path,
    *,
    references: dict[str, str] | None = None,
    pages: dict[str, str] | None = None,
    changelog: str | None = None,
) -> Path:
    references = (
        {"alpha.md": "# Alpha\n\nAlpha links to [beta](beta.md#two).\n"}
        | {"beta.md": "# Beta\n\n## One\n\n## Two\n"}
        if references is None
        else references
    )
    pages = (
        {
            "index.mdx": (
                f"---\ntitle: Home\ndescription: {DESC}\n---\n\nSee [a](/alpha).\n"
            )
        }
        if pages is None
        else pages
    )
    (root / "references").mkdir(parents=True)
    for name, text in references.items():
        (root / "references" / name).write_text(text, encoding="utf-8")
    (root / "docs").mkdir()
    for name, text in pages.items():
        path = root / "docs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (root / "CHANGELOG.md").write_text(
        changelog
        or "# Changelog\n\n## [Unreleased]\n\n- pending\n\n"
        "## [2.0.0] - 2026-10-02\n\n- Fixed [#12], see `[#13]`.\n\n"
        "## [1.0.0] - 2026-10-01\n\n- First.\n",
        encoding="utf-8",
    )
    nav = [Path(n).with_suffix("").as_posix() for n in pages]
    nav += [r.removesuffix(".md") for r in references]
    nav += ["cli/overview", "cli/tool", "changelog"]
    (root / "docs" / "docs.json").write_text(
        json.dumps({"name": "Dex", "navigation": {"pages": nav}}),
        encoding="utf-8",
    )
    return root


def make_builder(root: Path, **overrides):
    parser, shared = _parser()
    reference_names = sorted(p.name for p in (root / "references").glob("*.md"))
    options = {
        "repo_root": root,
        "parser_factory": lambda: parser,
        "shared_options_factory": lambda: shared,
        "group_order_factory": lambda: ["tool"],
        "list_tracked_files": list,
        "em_dash_check": lambda text: [],
        "reference_pages": tuple(
            site.ReferencePage(n, n.removesuffix(".md"), site.PageMeta(n, DESC))
            for n in reference_names
        ),
        "generated_pages": {
            "cli/overview": site.PageMeta("CLI", DESC),
            "cli/tool": site.PageMeta("tool", DESC),
            "changelog": site.PageMeta("Changelog", DESC),
        },
    }
    options.update(overrides)
    return site.SiteBuilder(**options)


def messages(problems) -> str:
    return "\n".join(str(p) for p in problems)


# --- The real repository ----------------------------------------------------


@pytest.fixture(scope="module")
def real_build():
    builder = site.SiteBuilder(require_help=True)
    problems = builder.build()
    return builder, problems


def test_the_repository_builds_with_no_problems(real_build):
    builder, problems = real_build
    assert not problems, messages(problems)
    assert builder.help_gaps == []


def test_every_reference_is_published_at_its_route_with_metadata(real_build):
    builder, _ = real_build
    for ref in site.REFERENCE_PAGES:
        page = builder.pages[ref.page]
        assert page.meta["title"] == ref.meta.title
        assert page.meta["description"] == ref.meta.description
        assert not page.body.lstrip().startswith("# "), f"{ref.source} kept its H1"
        assert f"references/{ref.source}" in page.body  # the source footer


def test_navigation_lists_exactly_the_built_pages(real_build):
    builder, _ = real_build
    navigation = site._navigation_pages(builder.config["navigation"])
    assert sorted(navigation) == sorted(builder.pages)


def test_every_cli_group_and_subcommand_has_a_page_and_a_heading(real_build):
    from exmergo_dex_core.cli import COMMAND_SURFACE

    builder, _ = real_build
    for group, subcommands in COMMAND_SURFACE.items():
        body = builder.pages[f"cli/{group}"].body
        for sub in subcommands:
            assert f"\n## dex {group} {sub}\n" in body
        # Shared options are documented once, on the overview, not per command.
        assert "`--repo-root" not in body
        assert "](/cli/overview#global-options)" in body
    overview = builder.pages["cli/overview"].body
    assert "`--repo-root DIR`" in overview
    assert "](/cli/explore)" in overview


def test_two_builds_are_byte_identical(tmp_path, real_build):
    builder, _ = real_build
    first, second = tmp_path / "a", tmp_path / "b"
    builder.write(first)
    site.SiteBuilder(require_help=True).build()
    again = site.SiteBuilder(require_help=True)
    again.build()
    again.write(second)
    files = sorted(p.relative_to(first) for p in first.rglob("*") if p.is_file())
    assert files == sorted(
        p.relative_to(second) for p in second.rglob("*") if p.is_file()
    )
    for rel in files:
        assert (first / rel).read_bytes() == (second / rel).read_bytes(), rel


# --- Mapping and metadata ---------------------------------------------------


def test_a_reference_without_a_mapping_row_fails(tmp_path):
    root = make_repo(tmp_path)
    builder = make_builder(root)
    (root / "references" / "gamma.md").write_text("# Gamma\n", encoding="utf-8")
    assert "references/gamma.md:1: has no row in REFERENCE_PAGES" in messages(
        builder.build()
    )


def test_a_mapping_row_without_a_file_fails(tmp_path):
    root = make_repo(tmp_path)
    rows = (
        *make_builder(root).reference_pages,
        site.ReferencePage("gone.md", "gone", site.PageMeta("Gone", DESC)),
    )
    problems = messages(make_builder(root, reference_pages=rows).build())
    assert "maps references/gone.md, which does not exist" in problems


@pytest.mark.parametrize("length", [139, 161])
def test_a_description_outside_the_range_fails(tmp_path, length):
    root = make_repo(
        tmp_path,
        pages={
            "index.mdx": f"---\ntitle: Home\ndescription: {'x' * length}\n---\n\nHi.\n"
        },
    )
    assert f"description is {length} characters" in messages(make_builder(root).build())


def test_a_reference_must_open_with_exactly_one_h1(tmp_path):
    root = make_repo(
        tmp_path,
        references={"alpha.md": "Intro first.\n", "beta.md": "# Beta\n\n# Again\n"},
    )
    problems = messages(make_builder(root).build())
    assert "references/alpha.md:1: the document must open with an H1" in problems
    assert "references/beta.md:3: a second H1 outside a code fence" in problems


def test_an_h1_inside_a_fence_is_not_an_h1():
    body, h1, problems = site.to_mdx(
        "# Title\n\n```yaml\n# .dex/config.yml\nconnector: duckdb\n```\n",
        source="x.md",
        lift_h1=True,
    )
    assert h1 == "Title" and not problems
    assert "# .dex/config.yml" in body


# --- Links ------------------------------------------------------------------


def test_links_are_rewritten_to_routes_and_repository_urls(tmp_path):
    root = make_repo(
        tmp_path,
        references={
            "alpha.md": (
                "# Alpha\n\n[b](beta.md) [b2](beta.md#two) [here](#local) "
                "[agents](../AGENTS.md) [dir](../skills)\n\n## Local\n"
            ),
            "beta.md": "# Beta\n\n## Two\n",
        },
    )
    (root / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    (root / "skills").mkdir()
    builder = make_builder(root)
    assert not builder.build(), messages(builder.problems)
    body = builder.pages["alpha"].body
    assert "[b](/beta)" in body and "[b2](/beta#two)" in body
    assert "[here](#local)" in body
    assert f"[agents]({site.GITHUB}/blob/main/AGENTS.md)" in body
    assert f"[dir]({site.GITHUB}/tree/main/skills)" in body


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("missing.md", "links to missing.md, which does not exist"),
        ("beta.md#nope", "links to beta.md#nope, but beta.md has no such heading"),
        ("#nope", "links to #nope, which is not a heading here"),
    ],
)
def test_a_broken_reference_link_fails_with_its_line(tmp_path, link, expected):
    root = make_repo(
        tmp_path,
        references={
            "alpha.md": f"# Alpha\n\nText.\n[x]({link})\n",
            "beta.md": "# Beta\n",
        },
    )
    assert f"references/alpha.md:3: {expected}" in messages(make_builder(root).build())


def test_hand_written_pages_link_by_route_only(tmp_path):
    root = make_repo(
        tmp_path,
        pages={
            "index.mdx": (
                f"---\ntitle: Home\ndescription: {DESC}\n---\n\n"
                "[ok](/alpha) [home](/) [rel](alpha.md) [gone](/nowhere)\n"
            )
        },
    )
    problems = messages(make_builder(root).build())
    assert "pages under docs/ link by site route, such as /guardrails/" in problems
    assert "links to /nowhere, not a page" in problems
    assert "/alpha," not in problems and "links to /," not in problems


def test_a_tracked_file_linking_an_unknown_docs_page_fails(tmp_path):
    root = make_repo(tmp_path)
    url = site.SITE + "/no-such-page"
    (root / "README.md").write_text(
        f"Docs: {url}, {site.SITE}/alpha and {site.SITE}\n", encoding="utf-8"
    )
    builder = make_builder(root, list_tracked_files=lambda: ["README.md"])
    problems = messages(builder.build())
    assert f"README.md:1: {url} is not a page on the site" in problems
    # A page and the home resolve.
    assert problems.count("is not a page") == 1


# --- Escaping ---------------------------------------------------------------


def test_a_link_whose_text_is_code_keeps_its_text(tmp_path):
    """A code span inside link text is held inside the link's own placeholder,
    so restoring once left the inner token in the page as invisible text."""

    root = make_repo(
        tmp_path,
        references={
            "alpha.md": (
                "# Alpha\n\nSee [`beta.md`](beta.md) and [the `x` key](beta.md).\n"
            ),
            "beta.md": "# Beta\n",
        },
    )
    builder = make_builder(root)
    assert not builder.build(), messages(builder.problems)
    body = builder.pages["alpha"].body
    assert "[`beta.md`](/beta)" in body
    assert "[the `x` key](/beta)" in body
    assert not any(c in body for c in "\x00\x01")


def test_every_page_shares_with_a_title_naming_exmergo_dex(tmp_path):
    root = make_repo(
        tmp_path,
        pages={
            "index.mdx": f"---\ntitle: Dex\ndescription: {DESC}\n---\n\nHi.\n",
            "own.mdx": (
                f"---\ntitle: Own\ndescription: {DESC}\n"
                "og:title: Chosen by hand\n---\n\nHi.\n"
            ),
        },
    )
    builder = make_builder(root)
    assert not builder.build(), messages(builder.problems)
    files = builder.rendered()
    assert "og:title: Exmergo Dex\ntwitter:title: Exmergo Dex\n" in files["index.mdx"]
    assert "og:title: alpha.md · Exmergo Dex\n" in files["alpha.mdx"]
    assert "og:title: Chosen by hand\n" in files["own.mdx"]
    assert "twitter:title: Chosen by hand · Exmergo Dex" not in files["own.mdx"]


def test_a_control_character_in_a_page_fails_the_build(tmp_path):
    root = make_repo(tmp_path, references={"alpha.md": "# Alpha\n\nBell \x07 here.\n"})
    assert "contains the control character '\\x07'" in messages(
        make_builder(root).build()
    )


def test_prose_is_escaped_and_code_is_left_alone():
    text = (
        "# T\n\nUse <name> and {x} for $5 or $6. See <https://example.com/a>.\n"
        "<!-- dex:generated:x:begin -->\n"
        "A code span `a <b> {c}\nwrapped` stays.\n\n"
        "```\n<raw> {raw} $raw\n```\n"
    )
    body, _, problems = site.to_mdx(text, source="x.md", lift_h1=True)
    assert not problems
    assert "Use &lt;name> and \\{x\\} for \\$5 or \\$6." in body
    assert "[https://example.com/a](https://example.com/a)" in body
    assert "dex:generated" not in body
    assert "`a <b> {c}\nwrapped`" in body
    assert "<raw> {raw} $raw" in body


def test_a_hand_written_page_may_use_only_known_components(tmp_path):
    root = make_repo(
        tmp_path,
        pages={
            "index.mdx": (
                f"---\ntitle: Home\ndescription: {DESC}\n---\n\n"
                "<Note>fine</Note>\n\n<div>not fine</div>\n\n`<code>` is fine.\n"
            )
        },
    )
    problems = messages(make_builder(root).build())
    assert "<div> is not an allowed component" in problems
    assert "<Note>" not in problems and "<code>" not in problems


# --- The CLI reference ------------------------------------------------------


def test_the_cli_renderer_documents_every_kind_of_argument():
    parser, shared = _parser()
    pages, gaps = site.render_cli_reference(parser, shared, group_order=["tool"])
    tool = pages["cli/tool"]
    assert "## dex tool run" in tool
    assert "[--yes | --no]" in tool
    assert "[--mode {a,b}]" in tool and "[<object> ...]" in tool
    assert "| `--tag TAG` | a tag; repeatable |" in tool
    assert "kept out of &lt;name>/" in tool
    assert "--repo-root" not in tool  # shared, documented on the overview
    overview = pages["cli/overview"]
    assert (
        "| `--repo-root REPO_ROOT` | where to run | before the group or after"
        in overview
    )
    assert (
        "| `--engine ENGINE` | how to build the engine | before the group only |"
        in overview
    )
    assert gaps == []


def test_cli_details_are_collapsed_without_hiding_command_anchors():
    parser, shared = _parser()
    pages, _ = site.render_cli_reference(parser, shared, group_order=["tool"])
    body = pages["cli/tool"]
    heading = body.index("## dex tool run")
    details = body.index('<Accordion title="Usage and options for dex tool run">')
    end = body.index("</Accordion>", details)
    assert "[`dex tool run`](#dex-tool-run)" in body[:heading]
    assert heading < details < body.index("| `--tag TAG` |") < end
    assert "```" in body[details:end]
    assert "[global options](/cli/overview#global-options)" in body[:heading]


def test_getting_started_and_workflows_are_separate_from_reference(real_build):
    builder, _ = real_build
    docs, reference, _ = builder.config["navigation"]["tabs"]
    docs_pages = site._navigation_pages(docs)
    reference_pages = site._navigation_pages(reference)
    assert docs_pages[:4] == ["index", "quickstart", "installation", "connect"]
    assert {"guides/explore", "guides/transform", "guides/maintain"} <= set(docs_pages)
    assert "reference/command-contract" in reference_pages
    assert "connectors/duckdb" in reference_pages
    assert "reference/command-contract" not in docs_pages
    assert 'href="/quickstart"' in builder.pages["index"].body
    assert 'href="/installation"' in builder.pages["index"].body


def test_missing_help_is_a_gap_and_a_problem_when_required(tmp_path):
    parser, shared = _parser()
    run = parser._subparsers._group_actions[0].choices["tool"]
    run._subparsers._group_actions[0].choices["run"].add_argument("--bare")
    _, gaps = site.render_cli_reference(parser, shared, group_order=["tool"])
    assert "dex tool run --bare: no help" in gaps
    root = make_repo(tmp_path)
    builder = make_builder(root, parser_factory=lambda: parser, require_help=True)
    assert "dex tool run --bare: no help" in messages(builder.build())


def test_a_dash_in_help_text_fails_the_em_dash_rule(tmp_path):
    parser, _ = _parser()
    parser.description = "First -- then second."
    root = make_repo(tmp_path)
    builder = make_builder(
        root, parser_factory=lambda: parser, em_dash_check=site._load_em_dash_check()
    )
    assert "em-dash rule: '--'" in messages(builder.build())


# --- The changelog ----------------------------------------------------------


def test_the_changelog_links_pull_requests_and_skips_unreleased():
    text = "# Changelog\n\n## [Unreleased]\n\n- pending\n\n" + "".join(
        f"## [1.0.{n}] - 2026-10-0{n}\n\n- Fixed [#{n}], not `[#9{n}]`.\n\n"
        for n in range(9, 0, -1)
    )
    body, problems = site.render_changelog(text, release=None, limit=3)
    assert not problems
    assert "pending" not in body
    assert '<Update label="1.0.9" description="2026-10-09">' in body
    assert "1.0.6" not in body  # past the limit
    assert f"[#9]({site.GITHUB}/pull/9)" in body
    assert "`[#99]`" in body  # not inside code
    assert "Older releases are in the full" in body


def test_the_changelog_must_lead_with_the_release_being_published():
    text = "# Changelog\n\n## [1.0.0] - 2026-10-01\n\n- First.\n"
    _, problems = site.render_changelog(text, release="v1.0.1")
    assert "not the release being published (v1.0.1)" in messages(problems)
    _, problems = site.render_changelog(text, release="v1.0.0")
    assert not problems


# --- Writing ----------------------------------------------------------------


def test_write_refuses_a_directory_that_holds_no_previous_build(tmp_path):
    root = make_repo(tmp_path / "repo")
    builder = make_builder(root)
    assert not builder.build(), messages(builder.problems)
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(SystemExit, match="refusing to clear"):
        builder.write(out)
    assert (out / "keep.txt").read_text(encoding="utf-8") == "mine"


def test_write_replaces_a_previous_build(tmp_path):
    root = make_repo(tmp_path / "repo")
    builder = make_builder(root)
    assert not builder.build(), messages(builder.problems)
    out = tmp_path / "out"
    builder.write(out)
    (out / "stale.mdx").write_text("old", encoding="utf-8")
    builder.write(out)
    assert not (out / "stale.mdx").exists()
    assert (out / "alpha.mdx").is_file() and (out / "docs.json").is_file()
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert readme.startswith(site.OUT_MARKER)


def test_write_never_clears_a_git_checkout(tmp_path):
    """A checkout of the docs-site branch carries the build marker, so the
    marker alone would let `--out` wipe a real repository."""

    root = make_repo(tmp_path / "repo")
    builder = make_builder(root)
    assert not builder.build(), messages(builder.problems)
    checkout = tmp_path / "docs-site"
    (checkout / ".git").mkdir(parents=True)
    (checkout / "README.md").write_text(site.OUT_MARKER + "\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="it is a git checkout"):
        builder.write(checkout)
    assert (checkout / ".git").is_dir() and (checkout / "README.md").is_file()


def test_the_site_ships_its_settings_menu_and_redirects(tmp_path):
    root = make_repo(tmp_path)
    builder = make_builder(root)
    assert not builder.build(), messages(builder.problems)
    config = json.loads(builder.rendered()["docs.json"])
    assert config["name"] == "Dex"  # every other setting is published as written
    assert "alpha" in config["navigation"]["pages"]
    assert "index" in config["navigation"]["pages"]
    redirects = config["redirects"]
    assert {"source": "/references/alpha.md", "destination": "/alpha"} in redirects
    assert {"source": "/references/alpha", "destination": "/alpha"} in redirects


def test_a_redirect_must_go_from_a_route_to_a_page(tmp_path):
    root = make_repo(tmp_path)
    path = root / "docs" / "docs.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["redirects"] = [
        {"source": "start", "destination": "/alpha"},
        {"source": "/old", "destination": "/nowhere"},
        {"source": "/alpha", "destination": "/beta"},
        # A wildcard names many pages, which Mintlify's own check resolves.
        {"source": "/dex/:slug*", "destination": "/:slug*"},
    ]
    path.write_text(json.dumps(config), encoding="utf-8")
    problems = messages(make_builder(root).build())
    assert "redirect source start is not a route" in problems
    assert "redirect /old goes to a missing page" in problems
    assert "redirect source /alpha is also a page" in problems
    assert "/dex/:slug*" not in problems


def test_the_site_config_must_carry_a_navigation(tmp_path):
    root = make_repo(tmp_path)
    (root / "docs" / "docs.json").write_text('{"name": "Dex"}', encoding="utf-8")
    assert "docs/docs.json:1: has no navigation" in messages(make_builder(root).build())


def test_private_material_never_reaches_a_page(tmp_path):
    root = make_repo(
        tmp_path,
        references={
            "alpha.md": "# Alpha\n\nTracked in SCRUM-1 and unversioned-docs.\n"
        },
    )
    problems = messages(make_builder(root).build())
    assert "names a ticket key" in problems
    assert "names the private planning folder" in problems


# --- Versions ---------------------------------------------------------------


def make_archive(root: Path) -> Path:
    """The `dex/` folder a release from before docs.trydex.ai built for the
    shared docs.exmergo.com: a legacy archive."""

    root.mkdir(parents=True)
    (root / "navigation.json").write_text(
        json.dumps({"product": "Dex", "pages": ["dex/index", "dex/alpha"]}),
        encoding="utf-8",
    )
    (root / "index.mdx").write_text(
        "---\ntitle: Dex\n---\n\nSee [Alpha](/dex/alpha#top), "
        '<Card href="/dex/alpha" />, [home](/dex) and [site](https://docs.exmergo.com/dex).\n',
        encoding="utf-8",
    )
    (root / "alpha.mdx").write_text(
        "---\ntitle: Alpha\n---\n\nOld.\n", encoding="utf-8"
    )
    (root / "images").mkdir()
    (root / "images" / "map.png").write_bytes(b"\x89PNG")
    (root / "redirects.json").write_text("[]", encoding="utf-8")
    (root / "README.md").write_text(site.OUT_MARKER + "\n", encoding="utf-8")
    return root


def test_archives_become_versions_beside_the_latest_routes(tmp_path):
    root = make_repo(tmp_path / "repo")
    archive = site.Archive("v1.14.1", make_archive(tmp_path / "v1.14"))
    builder = make_builder(root, archives=(archive,), latest="v1.15.2")
    assert not builder.build(), messages(builder.problems)

    navigation = json.loads(builder.rendered()["docs.json"])["navigation"]
    assert "pages" not in navigation
    latest, older = navigation["versions"]
    # The menu names minor releases; the exact tag is on the pages' notice.
    assert latest["version"] == "v1.15" and latest["default"] is True
    assert latest["tag"] == "Latest" and "tag" not in older
    assert "alpha" in latest["pages"]  # latest keeps the routes links point at
    # A legacy archive's dex/ prefix gives way to the label.
    assert older == {"version": "v1.14", "pages": ["v1.14/index", "v1.14/alpha"]}


def test_an_archived_page_stays_in_its_version_and_out_of_search(tmp_path):
    root = make_repo(tmp_path / "repo")
    archive = site.Archive("v1.14.1", make_archive(tmp_path / "v1.14"))
    builder = make_builder(root, archives=(archive,), latest="v1.15.2")
    assert not builder.build(), messages(builder.problems)
    out = tmp_path / "out"
    builder.write(out)

    page = (out / "v1.14" / "index.mdx").read_text(encoding="utf-8")
    assert page.startswith("---\nnoindex: true\ntitle: Dex\n---\n\n<Note>\n")
    assert "[Alpha](/v1.14/alpha#top)" in page
    assert '<Card href="/v1.14/alpha" />' in page
    assert "[home](/v1.14)" in page
    # A full URL is a deliberate link to a live site, and is left alone.
    assert "[site](https://docs.exmergo.com/dex)" in page
    assert (out / "v1.14" / "images" / "map.png").read_bytes() == b"\x89PNG"
    # Its menu is folded into the site's; its redirects and README stay behind.
    for left in ("navigation.json", "redirects.json", "README.md"):
        assert not (out / "v1.14" / left).exists()
    assert "noindex" not in (out / "alpha.mdx").read_text(encoding="utf-8")


def test_an_archive_built_as_a_whole_site_moves_under_its_label(tmp_path):
    old = tmp_path / "v1.16"
    old.mkdir()
    (old / "docs.json").write_text(
        json.dumps({"name": "Dex", "navigation": {"pages": ["index", "alpha"]}}),
        encoding="utf-8",
    )
    (old / "index.mdx").write_text(
        "---\ntitle: Dex\n---\n\nSee [Alpha](/alpha#top), "
        "[home](/) and [cdn](//cdn.example/x).\n",
        encoding="utf-8",
    )
    (old / "alpha.mdx").write_text("---\ntitle: Alpha\n---\n\nOld.\n", encoding="utf-8")
    (old / "README.md").write_text(site.OUT_MARKER + "\n", encoding="utf-8")
    archive = site.Archive("v1.16.3", old)
    builder = make_builder(
        make_repo(tmp_path / "repo"), archives=(archive,), latest="v1.17.0"
    )
    assert not builder.build(), messages(builder.problems)

    navigation = json.loads(builder.rendered()["docs.json"])["navigation"]
    assert navigation["versions"][1] == {
        "version": "v1.16",
        "pages": ["v1.16/index", "v1.16/alpha"],
    }
    files = builder.archived()
    page = files["v1.16/index.mdx"].decode("utf-8")
    assert "[Alpha](/v1.16/alpha#top)" in page and "[home](/v1.16/)" in page
    assert "[cdn](//cdn.example/x)" in page  # a protocol-relative URL is not a route
    # Its own settings are the latest site's to give, so they stay behind.
    assert "v1.16/docs.json" not in files and "v1.16/README.md" not in files


def test_an_archived_page_names_its_release_and_links_to_the_latest(tmp_path):
    root = make_repo(tmp_path / "repo")
    old = make_archive(tmp_path / "v1.14")
    (old / "gone.mdx").write_text("---\ntitle: Gone\n---\n\nOld.\n", encoding="utf-8")
    archive = site.Archive("v1.14.1", old)
    builder = make_builder(root, archives=(archive,), latest="v1.15.2")
    assert not builder.build(), messages(builder.problems)
    files = builder.archived()

    index = files["v1.14/index.mdx"].decode("utf-8")
    assert "You are reading the docs for dex v1.14 (v1.14.1)." in index
    assert "See the [latest version](/)." in index
    alpha = files["v1.14/alpha.mdx"].decode("utf-8")
    assert "See the [latest version](/alpha)." in alpha
    # A page the latest release no longer has sends the reader to its home.
    gone = files["v1.14/gone.mdx"].decode("utf-8")
    assert "See the [latest version](/)." in gone


def test_a_build_without_archives_has_no_version_menu(tmp_path):
    builder = make_builder(make_repo(tmp_path), latest="v1.15.2")
    assert not builder.build(), messages(builder.problems)
    config = json.loads(builder.rendered()["docs.json"])
    assert "versions" not in config["navigation"]


@pytest.mark.parametrize(
    ("release", "latest", "expected"),
    [
        ("1.14.1", "v1.15.2", "an archive is named by its release: vX.Y.Z"),
        ("v1.14", "v1.15.2", "an archive is named by its release: vX.Y.Z"),
        ("v1.15.0", "v1.15.2", "v1.15 is listed more than once"),
        ("v1.14.1", "v1.15", "the latest version is a release: vX.Y.Z"),
    ],
)
def test_an_archive_must_name_a_release_of_another_minor(
    tmp_path, release, latest, expected
):
    archive = site.Archive(release, make_archive(tmp_path / "old"))
    builder = make_builder(
        make_repo(tmp_path / "repo"), archives=(archive,), latest=latest
    )
    assert expected in messages(builder.build())


def test_an_archive_must_hold_every_page_its_menu_lists(tmp_path):
    old = make_archive(tmp_path / "old")
    (old / "alpha.mdx").unlink()
    builder = make_builder(
        make_repo(tmp_path / "repo"), archives=(site.Archive("v1.14.1", old),)
    )
    assert "its menu lists dex/alpha, which it lacks" in messages(builder.build())
    empty = tmp_path / "empty"
    empty.mkdir()
    builder = make_builder(
        make_repo(tmp_path / "repo2"), archives=(site.Archive("v1.14.1", empty),)
    )
    assert "holds no built site" in messages(builder.build())


def test_a_backfilled_release_drops_only_the_menu_pages_it_cannot_build():
    """A release from before the site lacks command groups added later, so its
    menu loses those pages, and a group left empty goes with them."""

    spec = importlib.util.spec_from_file_location(
        "build_docs_backfill", ROOT / "scripts" / "build_docs_backfill.py"
    )
    backfill = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backfill)
    menu = {
        "product": "Dex",
        "tabs": [
            {"tab": "Docs", "groups": [{"group": "Start", "pages": ["index"]}]},
            {
                "tab": "Reference",
                "groups": [
                    {"group": "CLI", "pages": ["cli/overview", "cli/config"]},
                    {"group": "Later", "pages": ["cli/viz"]},
                ],
            },
        ],
    }
    kept = backfill.prune(menu, {"index", "cli/overview"})
    assert kept == {
        "product": "Dex",
        "tabs": [
            {"tab": "Docs", "groups": [{"group": "Start", "pages": ["index"]}]},
            {
                "tab": "Reference",
                "groups": [{"group": "CLI", "pages": ["cli/overview"]}],
            },
        ],
    }
