"""Load a self-contained HTML page in headless Chromium (playwright): report console
errors, screenshot it, evaluate an expression and read an element's text after
wheel and hover events.

Usage: python tools/html_snapshot.py PAGE.html OUT_DIR [--wheel X,Y,DELTA ...]
       [--hover X,Y ...] [--text SELECTOR] [--canvas SELECTOR] [--eval EXPR]
"""

import argparse
import json
import pathlib

from playwright.sync_api import sync_playwright


def _point(text):
    return tuple(float(v) for v in text.split(","))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("page", type=pathlib.Path)
    ap.add_argument("out", type=pathlib.Path)
    ap.add_argument("--canvas", default="canvas")
    ap.add_argument("--hover", type=_point, nargs="*", default=[])
    ap.add_argument("--wheel", type=_point, nargs="*", default=[])
    ap.add_argument("--text", default="body")
    ap.add_argument("--width", type=int, default=1400)
    ap.add_argument("--eval", help="JavaScript expression whose JSON value is reported")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    report = {"errors": [], "hover": [], "eval": None}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": args.width, "height": 900})
        page.on(
            "console", lambda m: m.type == "error" and report["errors"].append(m.text)
        )
        page.on("pageerror", lambda e: report["errors"].append(str(e)))
        page.goto(args.page.resolve().as_uri())
        page.wait_for_function("document.readyState === 'complete'")
        page.wait_for_timeout(3000)
        if args.eval:
            report["eval"] = page.evaluate(args.eval)
        box = page.locator(args.canvas).first.bounding_box()
        page.screenshot(path=args.out / "page.png", full_page=True)
        for x, y, delta in args.wheel:
            page.mouse.move(box["x"] + x, box["y"] + y)
            page.mouse.wheel(0, delta)
            page.wait_for_timeout(300)
        if args.wheel:
            page.screenshot(path=args.out / "zoomed.png", full_page=True)
        for x, y in args.hover:
            page.mouse.move(box["x"] + x, box["y"] + y)
            page.wait_for_timeout(100)
            report["hover"].append(page.locator(args.text).inner_text())
        browser.close()
    (args.out / "report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
