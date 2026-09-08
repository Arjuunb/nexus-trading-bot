import { test, expect } from "@playwright/test";
import { mockApi } from "./mock";
import { NAV_LABELS, slug } from "../src/app-context";

const ROUTES = [...NAV_LABELS, "Settings"].map(slug);

test("current application routes remain contained on a phone viewport", async ({ page }, testInfo) => {
  await mockApi(page);
  await page.setViewportSize({ width: 390, height: 844 });

  for (const route of ROUTES) {
    await page.goto(`/#/${route}`);
    await page.waitForTimeout(1000);
    await expect(page.getByRole("heading", { name: "This page hit an error" })).toHaveCount(0);
    const overflow = await page.evaluate(() => {
      const content = document.querySelector<HTMLElement>(".content");
      const offenders = [...document.querySelectorAll<HTMLElement>(".content *")]
        .filter((element) => {
          const rect = element.getBoundingClientRect();
          const style = getComputedStyle(element);
          return style.display !== "none" && style.visibility !== "hidden"
            && rect.width > 0 && (rect.left < -1 || rect.right > window.innerWidth + 1);
        })
        .slice(0, 8)
        .map((element) => ({
          tag: element.tagName.toLowerCase(),
          className: element.className,
          width: Math.round(element.getBoundingClientRect().width),
          right: Math.round(element.getBoundingClientRect().right),
        }));
      return {
        document: document.documentElement.scrollWidth - window.innerWidth,
        content: content ? content.scrollWidth - content.clientWidth : 0,
        offenders,
      };
    });
    expect(
      { document: overflow.document, content: overflow.content },
      `${route} has horizontal viewport overflow: ${JSON.stringify(overflow.offenders)}`,
    ).toEqual({ document: 0, content: 0 });
    if (["dashboard", "price-action-lab", "paper-trading"].includes(route)) {
      await page.screenshot({ path: testInfo.outputPath(`${route}-phone.png`) });
    }
  }
});

test("dashboard metrics use one readable column on the smallest breakpoint", async ({ page }) => {
  await mockApi(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/#/dashboard");
  const columns = await page.locator(".metric-row").evaluate((element) =>
    getComputedStyle(element).gridTemplateColumns.split(" ").filter(Boolean).length,
  );
  expect(columns).toBe(1);
});

test("mobile pet stays in the footer and opens no box until requested", async ({ page }) => {
  await mockApi(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/#/dashboard");

  const content = await page.locator(".content").boundingBox();
  const pet = await page.locator(".nexus-pet-button").boundingBox();
  expect(content).not.toBeNull();
  expect(pet).not.toBeNull();
  expect(pet!.y).toBeGreaterThanOrEqual(content!.y + content!.height - 1);
  await expect(page.locator(".nexus-pet-popover")).toHaveCount(0);

  await page.locator(".nexus-pet-button").click();
  await expect(page.locator(".nexus-pet-popover")).toBeVisible();
  const popover = await page.locator(".nexus-pet-popover").boundingBox();
  expect(popover).not.toBeNull();
  expect(popover!.x).toBeGreaterThanOrEqual(0);
  expect(popover!.x + popover!.width).toBeLessThanOrEqual(390);
  expect(popover!.y + popover!.height).toBeLessThanOrEqual(content!.y + content!.height);
});

test("blocking modals and fullscreen charts render above persistent app chrome", async ({ page }) => {
  await mockApi(page);
  await page.setViewportSize({ width: 1440, height: 900 });

  await page.goto("/#/dashboard");
  await page.getByLabel("Account menu").click();
  await page.locator(".pm-logout").click();
  await expect(page.locator(".modal-overlay")).toHaveJSProperty("clientHeight", 900);
  const modalBlocksFooter = await page.locator(".nexus-pet-button").evaluate((pet) => {
    const box = pet.getBoundingClientRect();
    return Boolean(document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2)?.closest(".modal-overlay"));
  });
  expect(modalBlocksFooter).toBe(true);
  const modalZ = Number(await page.locator(".modal-overlay").evaluate((element) => getComputedStyle(element).zIndex));
  const headerZ = Number(await page.locator(".topbar").evaluate((element) => getComputedStyle(element).zIndex));
  const petZ = Number(await page.locator(".nexus-pet-root").evaluate((element) => getComputedStyle(element).zIndex));
  expect(modalZ).toBeGreaterThan(headerZ);
  expect(modalZ).toBeGreaterThan(petZ);
  await page.getByRole("dialog", { name: "Log out" }).getByRole("button", { name: "Cancel" }).click();
  await expect(page.locator(".modal-overlay")).toHaveCount(0);
  await page.getByLabel("Open command palette").click();
  await expect(page.locator(".command-overlay")).toHaveJSProperty("clientHeight", 900);
  await page.keyboard.press("Escape");

  await page.goto("/#/paper-trading");
  await page.getByTitle("Fullscreen").click();
  const chartZ = Number(await page.locator(".chart-full").evaluate((element) => getComputedStyle(element).zIndex));
  expect(chartZ).toBeGreaterThan(headerZ);

  await page.goto("/#/smc-visual-lab");
  await page.getByRole("button", { name: "Full screen", exact: true }).click();
  const smcZ = Number(await page.locator(".smc-fullscreen").evaluate((element) => getComputedStyle(element).zIndex));
  expect(smcZ).toBeGreaterThan(headerZ);
});

test("footer distinguishes authentication failures from network outages", async ({ page }) => {
  await mockApi(page);
  await page.route("http://localhost:8000/instances", (route) => route.fulfill({ status: 401, json: { detail: "Session expired" } }));
  await page.goto("/#/dashboard");

  await expect(page.locator(".ticker")).toContainText("AUTH_REQUIRED");
  await expect(page.locator(".ticker")).not.toContainText("backend not reachable");
});

test("lab footer never falls through to the global instance when its API fails", async ({ page }) => {
  await mockApi(page);
  await page.route("http://localhost:8000/research/price-action/bot-status", (route) =>
    route.fulfill({ status: 500, body: "" }),
  );
  await page.goto("/#/price-action-lab");

  await expect(page.locator(".ticker")).toContainText("PRICE ACTION LAB");
  await expect(page.locator(".ticker")).toContainText("API_ERROR · HTTP 500");
  await expect(page.locator(".ticker")).not.toContainText("Instance mode");
});

test("footer reports permission, API and network errors distinctly", async ({ page }) => {
  await mockApi(page);
  for (const [status, label] of [[403, "ACCESS_DENIED"], [503, "API_ERROR · HTTP 503"], [0, "BACKEND_UNREACHABLE"]] as const) {
    await page.route("http://localhost:8000/instances", (route) => status
      ? route.fulfill({ status, json: { detail: "Request unavailable" } })
      : route.abort("connectionrefused"));
    await page.goto("/#/dashboard");
    await page.reload();
    await expect(page.locator(".ticker")).toContainText(label);
    await page.unroute("http://localhost:8000/instances");
  }
});

test("header notification and account panels fit the viewport", async ({ page }) => {
  await mockApi(page);
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await page.goto("/#/dashboard");
    await page.getByRole("button", { name: /^Notifications/ }).click();
    await expect(page.locator(".notif-pop")).toBeVisible();
    await page.locator(".notif-pop").evaluate((panel) => Promise.all(panel.getAnimations().map((animation) => animation.finished)));
    const notification = await page.locator(".notif-pop").boundingBox();
    expect(notification!.x).toBeGreaterThanOrEqual(0);
    expect(notification!.x + notification!.width).toBeLessThanOrEqual(width);
    await page.keyboard.press("Escape");
    await page.getByLabel("Account menu").click();
    await expect(page.locator(".pm-panel")).toBeVisible();
    // Let the short opening transform settle before checking its edge.
    await page.locator(".pm-panel").evaluate((panel) => Promise.all(panel.getAnimations().map((animation) => animation.finished)));
    const account = await page.locator(".pm-panel").boundingBox();
    expect(account!.x).toBeGreaterThanOrEqual(0);
    expect(account!.x + account!.width).toBeLessThanOrEqual(width);
    await page.keyboard.press("Escape");
    await page.locator(".hdr-controls .hdr-chip").first().click();
    const modeMenu = page.getByRole("menu", { name: "Trading mode" });
    await expect(modeMenu).toBeVisible();
    await modeMenu.getByRole("button", { name: "Paper current" }).click({ trial: true });
    await page.keyboard.press("Escape");
  }
});

test("cached lab status is marked stale on failure and recovers after a successful poll", async ({ page }) => {
  await mockApi(page);
  let fail = false;
  await page.route("http://localhost:8000/research/price-action/bot-status", (route) => fail
    ? route.fulfill({ status: 503, json: { detail: "Persistence blocked" } })
    : route.fallback());
  await page.goto("/#/price-action-lab");
  await expect(page.locator(".ticker")).toContainText("SYNCHRONIZED");
  fail = true;
  await expect(page.locator(".ticker")).toContainText("DEGRADED · API_ERROR · HTTP 503", { timeout: 12000 });
  await expect(page.locator(".ticker")).toContainText("STALE");
  await expect(page.locator(".ticker")).not.toContainText("SYNCHRONIZED");
  fail = false;
  await expect(page.locator(".ticker")).toContainText("SYNCHRONIZED", { timeout: 15000 });
  await expect(page.locator(".ticker")).not.toContainText("DEGRADED");
});
