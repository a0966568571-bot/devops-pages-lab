"""Gate for the deploy pipeline: fail if the built site is not safe to publish.

Checks
  1. README.md has the activity markers, once each, START before END.
  2. Every built HTML page parses cleanly: doctype, <title>, balanced tags,
     no duplicate ids.
  3. Every internal link/asset resolves to a file in the built site.
  4. External links do not return 404/410 (other errors are warnings,
     so a slow or rate-limited remote does not block a deploy).
  5. version.txt in the built site matches the version this run built.

Usage
  python scripts/validate_site.py --site _site --readme README.md \
      --base-path /devops-pages-lab --expect-version "<sha> <run_id>"

Exit code 0 = pass, 1 = at least one error.  Stdlib only.
"""

import argparse
import os
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

START = "<!--START_SECTION:activity-->"
END = "<!--END_SECTION:activity-->"

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "source", "track", "wbr"}
# End tags HTML lets you leave out; a missing one is not an error.
OPTIONAL_END = {"p", "li", "dt", "dd", "option", "tr", "td", "th", "thead",
                "tbody", "tfoot", "html", "head", "body", "colgroup"}
LINK_ATTRS = {("a", "href"), ("link", "href"), ("script", "src"),
              ("img", "src"), ("source", "src")}
BROKEN_STATUS = {404, 410}


def check_readme(text):
    errors = []
    starts, ends = text.count(START), text.count(END)
    if starts != 1:
        errors.append("README.md: expected 1 START marker, found %d" % starts)
    if ends != 1:
        errors.append("README.md: expected 1 END marker, found %d" % ends)
    if starts == 1 and ends == 1 and text.index(START) > text.index(END):
        errors.append("README.md: END marker comes before START marker")
    return errors


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.errors = []
        self.links = []
        self.ids = set()
        self.has_doctype = False
        self.has_title = False

    def handle_decl(self, decl):
        if decl.lower().startswith("doctype"):
            self.has_doctype = True

    def handle_starttag(self, tag, attrs):
        self._attrs(tag, attrs)
        if tag == "title":
            self.has_title = True
        if tag not in VOID:
            self.stack.append((tag, self.getpos()[0]))

    def handle_startendtag(self, tag, attrs):
        self._attrs(tag, attrs)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        open_tags = [t for t, _ in self.stack]
        if tag not in open_tags:
            self.errors.append("line %d: stray </%s>" % (self.getpos()[0], tag))
            return
        while self.stack:
            t, line = self.stack.pop()
            if t == tag:
                break
            if t not in OPTIONAL_END:
                self.errors.append("line %d: <%s> never closed" % (line, t))

    def _attrs(self, tag, attrs):
        for name, value in attrs:
            if name == "id" and value:
                if value in self.ids:
                    self.errors.append("line %d: duplicate id=%r"
                                       % (self.getpos()[0], value))
                self.ids.add(value)
            if (tag, name) in LINK_ATTRS and value:
                self.links.append((self.getpos()[0], value))

    def finish(self):
        self.close()
        for t, line in self.stack:
            if t not in OPTIONAL_END:
                self.errors.append("line %d: <%s> never closed" % (line, t))
        if not self.has_doctype:
            self.errors.append("missing <!doctype html>")
        if not self.has_title:
            self.errors.append("missing <title>")


def page_url(site, path, base_path):
    """Public URL path of a built file, e.g. /devops-pages-lab/index.html."""
    rel = os.path.relpath(path, site).replace(os.sep, "/")
    return base_path.rstrip("/") + "/" + rel


def resolve_internal(site, base_path, url_path):
    """Map a URL path on this site to a file, or None if it does not exist."""
    base = base_path.rstrip("/") + "/"
    if not (url_path + "/").startswith(base):
        return None
    rel = url_path[len(base):]
    candidates = [rel]
    if rel == "" or rel.endswith("/"):
        candidates = [rel + "index.html"]
    elif "." not in rel.rsplit("/", 1)[-1]:
        candidates = [rel + ".html", rel + "/index.html"]
    for c in candidates:
        full = os.path.join(site, *c.split("/"))
        if os.path.isfile(full):
            return full
    return None


def check_external(url, retries=3, timeout=10, sleep=time.sleep):
    """Return (status, message). status is 'ok', 'broken' or 'warn'."""
    last = ""
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": "site-gate"})
        try:
            with urllib.request.urlopen(req, timeout=timeout):
                return "ok", ""
        except urllib.error.HTTPError as e:
            if e.code in BROKEN_STATUS:
                return "broken", "HTTP %d" % e.code
            last = "HTTP %d" % e.code
        except (urllib.error.URLError, OSError) as e:
            last = str(getattr(e, "reason", e))
        sleep(2 ** attempt)
    return "warn", last


def validate_site(site, base_path, check_ext=True, fetch=check_external):
    errors, warnings = [], []
    external = {}
    pages = 0
    for root, _, files in os.walk(site):
        for name in sorted(files):
            if not name.endswith(".html"):
                continue
            pages += 1
            path = os.path.join(root, name)
            rel = os.path.relpath(path, site)
            parser = PageParser()
            with open(path, encoding="utf-8") as f:
                parser.feed(f.read())
            parser.finish()
            errors += ["%s: %s" % (rel, e) for e in parser.errors]
            here = page_url(site, path, base_path)
            for line, link in parser.links:
                parts = urlsplit(urljoin(here, link))
                if parts.scheme in ("http", "https") and parts.netloc:
                    external.setdefault(link.split("#")[0], "%s:%d" % (rel, line))
                elif parts.scheme in ("", "file") and parts.path:
                    if resolve_internal(site, base_path, parts.path) is None:
                        errors.append("%s:%d: broken internal link %s"
                                      % (rel, line, link))
    if pages == 0:
        errors.append("no HTML pages found in %s" % site)
    if check_ext:
        for url, where in sorted(external.items()):
            status, msg = fetch(url)
            if status == "broken":
                errors.append("%s: broken external link %s (%s)" % (where, url, msg))
            elif status == "warn":
                warnings.append("%s: could not verify %s (%s)" % (where, url, msg))
    return errors, warnings, pages, len(external)


def check_version(site, expected):
    path = os.path.join(site, "version.txt")
    if not os.path.isfile(path):
        return ["version.txt missing from built site"]
    with open(path, encoding="utf-8") as f:
        got = f.read().strip()
    if got != expected:
        return ["version.txt is %r, expected %r" % (got, expected)]
    return []


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--site", default="_site")
    ap.add_argument("--readme", default="README.md")
    ap.add_argument("--base-path", default="/")
    ap.add_argument("--expect-version")
    ap.add_argument("--no-external", action="store_true")
    args = ap.parse_args(argv)

    with open(args.readme, encoding="utf-8") as f:
        errors = check_readme(f.read())
    site_errors, warnings, pages, ext = validate_site(
        args.site, args.base_path, check_ext=not args.no_external)
    errors += site_errors
    if args.expect_version:
        errors += check_version(args.site, args.expect_version)

    print("Checked %d page(s), %d external link(s)." % (pages, ext))
    for w in warnings:
        print("::warning::" + w)
    for e in errors:
        print("::error::" + e)
    if errors:
        print("GATE FAILED: %d error(s). Nothing will be deployed." % len(errors))
        return 1
    print("GATE PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
