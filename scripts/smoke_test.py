"""Smoke test after deploy: the live site must serve the version we just built.

HTTP 200 alone is not enough, because the old version also returns 200.
So we read <site>/version.txt and compare it with the expected version,
retrying for a while because the Pages CDN can lag behind the deploy.

Usage
  python scripts/smoke_test.py --url https://user.github.io/repo/ \
      --expect-version "<sha> <run_id>" [--attempts 20 --delay 15]

Exit code 0 = live site serves the expected version, 1 = it does not.
"""

import argparse
import sys
import time
import urllib.error
import urllib.request


def fetch(url, timeout=15):
    """Return (status, body). Bypasses caches with a query string and header."""
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(
        "%s%sts=%d" % (url, sep, int(time.time() * 1000)),
        headers={"Cache-Control": "no-cache", "User-Agent": "smoke-test"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, OSError) as e:
        return 0, str(getattr(e, "reason", e))


def smoke(base_url, expected, attempts=20, delay=15, get=fetch, sleep=time.sleep):
    base = base_url.rstrip("/") + "/"
    served = None
    for i in range(1, attempts + 1):
        status, body = get(base + "version.txt")
        served = body.strip() if status == 200 else "HTTP %d" % status
        print("attempt %d/%d: live version.txt = %r" % (i, attempts, served))
        if status == 200 and served == expected:
            home_status, home = get(base)
            if home_status == 200 and "Recent activity" in home:
                print("SMOKE TEST PASSED: live site serves %r" % expected)
                return True
            print("homepage check failed (HTTP %d)" % home_status)
        if i < attempts:
            sleep(delay)
    print("::error::SMOKE TEST FAILED: expected %r, live site serves %r"
          % (expected, served))
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", required=True)
    ap.add_argument("--expect-version", required=True)
    ap.add_argument("--attempts", type=int, default=20)
    ap.add_argument("--delay", type=float, default=15)
    args = ap.parse_args(argv)
    ok = smoke(args.url, args.expect_version, args.attempts, args.delay)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
