import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import smoke_test  # noqa: E402
import validate_site as vs  # noqa: E402

GOOD_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Home</title>
<link rel="stylesheet" href="/lab/assets/style.css?v=1"></head>
<body><h1 id="top">Hi</h1><p>text<p>more
<ul><li><a href="https://example.com/ok">ok</a><li><a href="#top">top</a></ul>
<a href="notes/">notes</a> <a href="mailto:a@b.c">mail</a>
</body></html>
"""


def build_site(tmp, pages):
    for rel, text in pages.items():
        path = os.path.join(tmp, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


def ok_fetch(url):
    return "ok", ""


class ReadmeMarkers(unittest.TestCase):
    def test_both_markers_in_order(self):
        self.assertEqual(vs.check_readme("x\n%s\n%s\n" % (vs.START, vs.END)), [])

    def test_missing_end(self):
        errs = vs.check_readme(vs.START)
        self.assertEqual(len(errs), 1)
        self.assertIn("END", errs[0])

    def test_duplicate_start(self):
        self.assertTrue(vs.check_readme(vs.START * 2 + vs.END))

    def test_wrong_order(self):
        errs = vs.check_readme(vs.END + vs.START)
        self.assertIn("before", errs[0])


class HtmlChecks(unittest.TestCase):
    def parse(self, text):
        p = vs.PageParser()
        p.feed(text)
        p.finish()
        return p.errors

    def test_good_page_has_no_errors(self):
        self.assertEqual(self.parse(GOOD_PAGE), [])

    def test_unclosed_div(self):
        errs = self.parse("<!doctype html><title>t</title><div><span></span>")
        self.assertTrue(any("<div> never closed" in e for e in errs))

    def test_stray_end_tag(self):
        errs = self.parse("<!doctype html><title>t</title></section>")
        self.assertTrue(any("stray </section>" in e for e in errs))

    def test_missing_doctype_and_title(self):
        errs = self.parse("<p>hi</p>")
        self.assertIn("missing <!doctype html>", errs)
        self.assertIn("missing <title>", errs)

    def test_duplicate_id(self):
        errs = self.parse('<!doctype html><title>t</title><a id="x"></a><b id="x"></b>')
        self.assertTrue(any("duplicate id" in e for e in errs))


class SiteLinks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.site = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_good_site_passes(self):
        build_site(self.site, {"index.html": GOOD_PAGE,
                               "assets/style.css": "",
                               "notes/index.html": "<!doctype html><title>n</title>"})
        errors, warnings, pages, ext = vs.validate_site(self.site, "/lab", fetch=ok_fetch)
        self.assertEqual(errors, [])
        self.assertEqual((pages, ext), (2, 1))

    def test_broken_internal_link_fails(self):
        build_site(self.site, {"index.html": GOOD_PAGE, "assets/style.css": ""})
        errors, _, _, _ = vs.validate_site(self.site, "/lab", check_ext=False)
        self.assertEqual(len(errors), 1)
        self.assertIn("broken internal link notes/", errors[0])

    def test_extensionless_link_resolves_to_html(self):
        page = "<!doctype html><title>t</title><a href='about'>a</a>"
        build_site(self.site, {"index.html": page, "about.html": page})
        errors, _, _, _ = vs.validate_site(self.site, "/", check_ext=False)
        self.assertEqual(errors, [])

    def test_link_outside_base_path_fails(self):
        page = "<!doctype html><title>t</title><a href='/other/x.html'>x</a>"
        build_site(self.site, {"index.html": page})
        errors, _, _, _ = vs.validate_site(self.site, "/lab", check_ext=False)
        self.assertEqual(len(errors), 1)

    def test_external_404_is_error_but_429_is_warning(self):
        page = ("<!doctype html><title>t</title>"
                "<a href='https://x.test/gone'>a</a><a href='https://x.test/busy'>b</a>")
        build_site(self.site, {"index.html": page})

        def fetch(url):
            return ("broken", "HTTP 404") if url.endswith("gone") else ("warn", "HTTP 429")

        errors, warnings, _, _ = vs.validate_site(self.site, "/", fetch=fetch)
        self.assertEqual(len(errors), 1)
        self.assertIn("gone", errors[0])
        self.assertEqual(len(warnings), 1)

    def test_empty_site_fails(self):
        errors, _, _, _ = vs.validate_site(self.site, "/", check_ext=False)
        self.assertTrue(errors)


class Version(unittest.TestCase):
    def test_match_and_mismatch(self):
        with tempfile.TemporaryDirectory() as site:
            self.assertTrue(vs.check_version(site, "abc 1"))  # missing file
            build_site(site, {"version.txt": "abc 1\n"})
            self.assertEqual(vs.check_version(site, "abc 1"), [])
            self.assertTrue(vs.check_version(site, "abc 2"))


class Smoke(unittest.TestCase):
    # smoke() prints "::error::" lines; GitHub would turn them into real
    # annotations on the test job, so keep the output inside the test.
    def setUp(self):
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def fake(self, versions):
        calls = iter(versions)

        def get(url):
            if url.endswith("version.txt"):
                return 200, next(calls)
            return 200, "<h2>Recent activity</h2>"
        return get

    def test_passes_when_new_version_appears(self):
        get = self.fake(["old 1\n", "new 2\n"])
        self.assertTrue(smoke_test.smoke("https://x/", "new 2", attempts=3,
                                         get=get, sleep=lambda s: None))

    def test_fails_when_old_version_stays(self):
        get = self.fake(["old 1"] * 3)
        self.assertFalse(smoke_test.smoke("https://x/", "new 2", attempts=3,
                                          get=get, sleep=lambda s: None))

    def test_200_without_activity_section_fails(self):
        def get(url):
            return (200, "new 2") if url.endswith("version.txt") else (200, "blank")
        self.assertFalse(smoke_test.smoke("https://x/", "new 2", attempts=2,
                                          get=get, sleep=lambda s: None))


if __name__ == "__main__":
    unittest.main()
