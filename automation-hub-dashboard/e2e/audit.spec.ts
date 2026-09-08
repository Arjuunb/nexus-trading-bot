import { test, expect, type Page, type ConsoleMessage } from "@playwright/test";
import { mockApi } from "./mock";
import { NAV_LABELS, slug } from "../src/app-context";

const PAGES = [...NAV_LABELS, "Settings"];

// console errors that are noise (not app defects) — network aborts from the
// polling hooks racing a page change, favicon, etc.
const IGNORE = [/Failed to load resource/i, /net::ERR_ABORTED/i, /favicon/i,
  /ResizeObserver/i, /Download the React DevTools/i];
function watchConsole(page: Page): string[] {
  const errs: string[] = [];
  page.on("console", (m: ConsoleMessage) => {
    if (m.type() === "error" && !IGNORE.some((r) => r.test(m.text()))) errs.push(m.text());
  });
  page.on("pageerror", (e) => errs.push("PAGEERROR: " + e.message));
  return errs;
}

test.describe("clickability audit — every page renders without JS errors", () => {
  for (const label of PAGES) {
    test(`page "${label}" loads clean`, async ({ page }) => {
      await mockApi(page);
      const errs = watchConsole(page);
      await page.goto(`/#/${slug(label)}`);
      await page.waitForTimeout(1200);            // let hooks fetch + render
      // the page shell must be present (sidebar + a heading somewhere)
      await expect(page.locator("aside.sidebar")).toBeVisible();
      await expect(page.locator(".topbar .page-title")).toHaveText(label);
      expect(errs, `console errors on ${label}:\n${errs.join("\n")}`).toHaveLength(0);
    });
  }
});

test.describe("clickability audit — interactive elements are sound", () => {
  for (const label of PAGES) {
    test(`page "${label}" — accessible names + not covered`, async ({ page }) => {
      await mockApi(page);
      await page.goto(`/#/${slug(label)}`);
      await page.waitForTimeout(1000);

      // collect every visible interactive element
      const els = page.locator(
        'button:visible, a[href]:visible, [role="button"]:visible, input[type="submit"]:visible',
      );
      const n = await els.count();
      const unnamed: string[] = [];
      const covered: string[] = [];

      for (let i = 0; i < n; i++) {
        const el = els.nth(i);
        // accessible name: text, aria-label, or title
        const name = (
          (await el.textContent())?.trim() ||
          (await el.getAttribute("aria-label")) ||
          (await el.getAttribute("title")) || ""
        ).trim();
        const cls = (await el.getAttribute("class")) || "";
        if (!name) unnamed.push(cls || "(no class)");

        // not covered: the element at its own center is itself or a descendant
        const obstruction = await el.evaluate((node) => {
          if ((node as HTMLButtonElement).disabled) return null;
          let box = node.getBoundingClientRect();
          let left = Math.max(0, box.left), right = Math.min(innerWidth, box.right);
          let top = Math.max(0, box.top), bottom = Math.min(innerHeight, box.bottom);
          // Only check the visible portion of a control; tables and the content
          // pane deliberately scroll and may clip controls outside their view.
          for (let parent = node.parentElement; parent; parent = parent.parentElement) {
            const style = getComputedStyle(parent);
            box = parent.getBoundingClientRect();
            if (/(auto|scroll|hidden|clip)/.test(style.overflowX)) {
              left = Math.max(left, box.left); right = Math.min(right, box.right);
            }
            if (/(auto|scroll|hidden|clip)/.test(style.overflowY)) {
              top = Math.max(top, box.top); bottom = Math.min(bottom, box.bottom);
            }
          }
          if (right - left < 2 || bottom - top < 2) return null;
          const hit = document.elementFromPoint((left + right) / 2, (top + bottom) / 2);
          return hit && node.contains(hit) ? null : `${hit?.tagName}.${hit?.className}`;
        });
        if (obstruction) covered.push(`${name || cls} covered by ${obstruction}`);
      }

      expect(unnamed, `unnamed interactive elements on ${label}: ${unnamed.join(", ")}`).toHaveLength(0);
      expect(covered, `covered controls on ${label}: ${covered.join(", ")}`).toHaveLength(0);
    });
  }
});
