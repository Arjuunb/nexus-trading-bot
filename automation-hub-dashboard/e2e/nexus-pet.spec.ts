import { expect, test, type Page } from "@playwright/test";
import { mockApi } from "./mock";

const STORAGE_KEY = "tradelogx:nexus-pet:v1";

test("Nexus pet migrates legacy choices and exposes the premium companion roster", async ({ page }) => {
  await mockApi(page);
  await page.addInitScript(({ key }) => {
    window.localStorage.setItem(key, JSON.stringify({ pet: "fireball", size: "large" }));
  }, { key: STORAGE_KEY });

  await page.goto("/#/overview");

  const pet = page.getByRole("button", { name: /Volt, Nexus pet:/ });
  await expect(pet).toBeVisible();
  await expect(pet.locator(".nexus-pet-laptop")).toBeVisible();
  await expect(pet.locator(".nexus-pet-laptop-chart")).toBeVisible();

  await pet.click();
  await page.getByRole("button", { name: "Open pet settings" }).click();

  const settings = page.getByRole("dialog", { name: "Nexus pet settings" });
  const roster = settings.getByRole("radiogroup", { name: "Pick a Nexus pet" });
  const expectedNames = ["Sprig", "Pulse", "Orbit", "Glint", "Echo", "Nova", "Volt", "Kiro"];
  await expect(roster.getByRole("radio")).toHaveCount(expectedNames.length);
  for (const name of expectedNames) {
    await expect(roster.getByRole("radio", { name: new RegExp(`^${name}`) })).toBeVisible();
  }

  await roster.getByRole("radio", { name: /^Sprig/ }).click();
  await page.getByRole("button", { name: "Close pet settings" }).click();

  const sprig = page.getByRole("button", { name: /Sprig, Nexus pet:/ });
  const productionSprite = sprig.locator(".nexus-pet-production-sprite");
  const productionImage = productionSprite.locator("img");
  await expect(productionSprite).toBeVisible();
  await expect(productionImage).toHaveAttribute(
    "src",
    /nexus-pet-concepts\/sprig-production-poses-v4\.png$/,
  );
  await expect.poll(() => productionImage.evaluate((image: HTMLImageElement) => image.complete && image.naturalWidth)).toBe(2480);
  await expect(sprig.locator("xpath=ancestor::div[contains(@class, 'nexus-pet-root')]")).toHaveAttribute("data-sprite-loaded", "true");
  await sprig.hover();
  await expect(sprig.locator("xpath=ancestor::div[contains(@class, 'nexus-pet-root')]")).toHaveAttribute("data-hovered", "true");

  const stored = await page.evaluate((key) => window.localStorage.getItem(key), STORAGE_KEY);
  expect(JSON.parse(stored ?? "null")).toEqual({ pet: "sprig", size: "large" });
});

test("each Sprig pose sits alone in its own cell, so no neighbouring pose shows at the edge", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/overview");
  const sprig = page.getByRole("button", { name: /Sprig, Nexus pet:/ });
  const image = sprig.locator(".nexus-pet-production-sprite img");
  await expect.poll(() => image.evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth)).toBe(2480);
  // The artwork: four equal cells whose edge columns are empty.
  const edges = await image.evaluate((img: HTMLImageElement) => {
    const canvas = document.createElement("canvas");
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext("2d")!;
    ctx.drawImage(img, 0, 0);
    const cell = img.naturalWidth / 4;
    const maxAlpha = (x0: number, x1: number) => {
      const { data } = ctx.getImageData(x0, 0, x1 - x0, img.naturalHeight);
      let max = 0;
      for (let i = 3; i < data.length; i += 4) max = Math.max(max, data[i]);
      return max;
    };
    return [0, 1, 2, 3].map((i) => [maxAlpha(i * cell, i * cell + 12), maxAlpha((i + 1) * cell - 12, (i + 1) * cell)]);
  });
  for (const [left, right] of edges) {
    expect(left).toBeLessThanOrEqual(16);
    expect(right).toBeLessThanOrEqual(16);
  }
  // The frame has the cell's shape, so the art is neither stretched nor cropped.
  const box = await sprig.boundingBox();
  expect(box!.width / box!.height).toBeCloseTo(620 / 724, 2);
});

test("Sprig rests on the footer as part of the platform, never on a shadow plate", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/overview");
  const sprig = page.getByRole("button", { name: /Sprig, Nexus pet:/ });
  await expect(sprig).toBeVisible();
  // No filter anywhere on the pet: WebKit draws a filter here as a box.
  for (const selector of [".nexus-pet-button", ".nexus-pet-stage", ".nexus-pet-production-sprite", ".nexus-pet-production-image"]) {
    const filter = await page.locator(`.nexus-pet-root ${selector}`).first().evaluate((el) => getComputedStyle(el).filter);
    expect(filter, selector).toBe("none");
  }
  // The art's lowest solid pixel (64/724 of the frame above its bottom) is on the footer line.
  const frame = (await sprig.boundingBox())!;
  const footer = (await page.locator("footer.ticker").boundingBox())!;
  const artBottom = frame.y + frame.height - frame.height * 64 / 724;
  expect(Math.abs(artBottom - footer.y)).toBeLessThanOrEqual(1.5);
});

// ---- Sprig behaves like a pet ------------------------------------------------
const INSTANCE = { id: "pet-i1", symbol: "BTCUSDT", strategy_label: "3-Candle Rejection", timeframe: "5m",
  state: "running", market_data: { market_data_status: "healthy" }, engine: { lifecycle_state: "running" } };
const snapshot = (state: string) => ({ instances: [{ ...INSTANCE, state, last_error: state === "error" ? "order rejected" : null }],
  active_slots: 1, max_active_slots: 8, global_risk_status: "ok" });

async function openSprig(page: Page, instances?: unknown) {
  await mockApi(page);
  if (instances) await page.route((url) => url.pathname === "/instances", (route) => route.fulfill({ json: instances }));
  await page.goto("/#/overview");
  const root = page.locator(".nexus-pet-root");
  await expect(root).toHaveAttribute("data-sprite-loaded", "true");
  const button = page.getByRole("button", { name: /Sprig, Nexus pet:/ });
  const box = (await button.boundingBox())!;
  return { root, button, cx: box.x + box.width / 2, cy: box.y + box.height / 2 };
}
const ledX = (page: Page) => page.locator(".nexus-pet-socket-l .nexus-pet-led").evaluate(
  (el) => parseFloat(getComputedStyle(el).translate.split(" ")[0]) || 0);

test("Sprig notices the pointer, looks at it, and waves when touched", async ({ page }) => {
  const { root, button, cx, cy } = await openSprig(page, snapshot("running"));
  await expect(root).toHaveAttribute("data-state", "running");
  await page.mouse.move(5, 5);
  await expect(root).toHaveAttribute("data-pose", "working");
  await expect(root).toHaveAttribute("data-eyes", "open");

  await page.mouse.move(cx - 120, cy, { steps: 4 });          // near, to the left: looks up and left
  await expect(root).toHaveAttribute("data-pose", "aware");
  await expect.poll(() => ledX(page)).toBeLessThan(0);
  await page.mouse.move(cx + 110, cy - 20, { steps: 6 });     // to the right: the eyes follow
  await expect.poll(() => ledX(page)).toBeGreaterThan(0);

  await button.hover();                                        // on Sprig: waves with its painted happy eyes
  await expect(root).toHaveAttribute("data-pose", "greet");
  await expect(page.locator(".nexus-pet-live-eyes")).toBeHidden();
  await page.mouse.move(5, 5, { steps: 4 });
  await expect(root).toHaveAttribute("data-pose", "working");
});

test("stroking or holding Sprig pets it, without opening the status panel", async ({ page }) => {
  const { root, cx, cy } = await openSprig(page, snapshot("running"));
  const status = page.getByRole("dialog", { name: "Nexus Engine status" });

  await page.mouse.move(cx, cy);
  for (const x of [cx - 26, cx + 26, cx - 26, cx + 26]) await page.mouse.move(x, cy, { steps: 5 });
  await expect(root).toHaveAttribute("data-petted", "true");
  await expect(root).toHaveAttribute("data-pose", "greet");
  const heart = page.locator(".nexus-pet-hearts svg").first();
  await expect.poll(() => heart.evaluate((el) => getComputedStyle(el).animationName)).toBe("nexus-pet-heart");
  await expect(status).toBeHidden();
  await expect(root).toHaveAttribute("data-petted", "false", { timeout: 4000 });

  await page.mouse.move(cx, cy);
  await page.mouse.down();                                     // press and hold: a pat, not a click
  await page.waitForTimeout(650);
  await page.mouse.up();
  await expect(root).toHaveAttribute("data-petted", "true");
  await expect(status).toBeHidden();

  await page.mouse.click(cx, cy);                              // a plain click still opens status
  await expect(status).toBeVisible();
});

test("Sprig dozes when nothing is running and wakes to greet you", async ({ page }) => {
  const { root, button } = await openSprig(page);              // the default mock: no running instance
  await page.mouse.move(5, 5);
  await expect(root).toHaveAttribute("data-state", "offline");
  await expect(root).toHaveAttribute("data-eyes", "sleep");
  await expect.poll(() => page.locator(".nexus-pet-zzz i").first()
    .evaluate((el) => getComputedStyle(el).animationName)).toBe("nexus-pet-z");
  await button.hover();
  await expect(root).toHaveAttribute("data-pose", "greet");
  await expect(root).toHaveAttribute("data-eyes", "open");
});

test("Sprig is never happy while the engine reports a problem", async ({ page }) => {
  const { root, cx, cy } = await openSprig(page, snapshot("error"));
  await expect(root).toHaveAttribute("data-state", "error");
  await expect(root).toHaveAttribute("data-pose", "alert");
  await page.mouse.move(cx, cy);
  for (const x of [cx - 26, cx + 26, cx - 26, cx + 26]) await page.mouse.move(x, cy, { steps: 5 });
  await page.mouse.down();
  await page.waitForTimeout(650);
  await page.mouse.up();
  await expect(root).toHaveAttribute("data-pose", "alert");    // hovered and petted, still alert
  expect(await root.getAttribute("data-petted")).not.toBe("true");
});

test("Sprig blinks on its own, and keeps still with reduced motion", async ({ page }) => {
  const { root } = await openSprig(page, snapshot("running"));
  await page.mouse.move(5, 5);
  const countBlinks = () => page.evaluate(() => {
    const w = window as unknown as { blinks?: number };
    if (w.blinks === undefined) {
      w.blinks = 0;
      new MutationObserver(() => {
        if (document.querySelector(".nexus-pet-root")?.getAttribute("data-blink") === "true") w.blinks! += 1;
      }).observe(document.querySelector(".nexus-pet-root")!, { attributes: true, attributeFilter: ["data-blink"] });
    }
    return w.blinks;
  });
  await countBlinks();
  await expect.poll(countBlinks, { timeout: 9000 }).toBeGreaterThan(0);   // a blink lasts 150ms

  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.reload();
  await expect(root).toHaveAttribute("data-sprite-loaded", "true");
  await page.waitForTimeout(4500);                             // the first blink would be due by now
  expect(await root.getAttribute("data-blink")).toBeNull();
});

test.describe("on a phone", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });

  test("Sprig sits on the footer as on desktop, and nothing stays hidden behind it", async ({ page }) => {
    const { button } = await openSprig(page, snapshot("running"));
    const frame = (await button.boundingBox())!;
    const footer = (await page.locator("footer.ticker").boundingBox())!;
    expect(frame.height).toBeCloseTo(72, 0);                          // three quarters of desktop, not a footer icon
    const artBottom = frame.y + frame.height - frame.height * 64 / 724;
    expect(Math.abs(artBottom - footer.y)).toBeLessThanOrEqual(1.5);  // resting on the footer line
    expect(frame.x + frame.width).toBeLessThanOrEqual(390);
    const padding = await page.locator(".content").evaluate((el) => parseFloat(getComputedStyle(el).paddingBottom));
    expect(padding).toBeGreaterThanOrEqual(footer.y - frame.y);      // the last card can scroll clear of it
    const touch = await button.evaluate((el) => ({ callout: getComputedStyle(el).getPropertyValue("-webkit-touch-callout"),
                                                     select: getComputedStyle(el).userSelect }));
    expect(touch.select).toBe("none");

    await button.tap();                                               // a tap opens status above Sprig, on screen
    const status = page.getByRole("dialog", { name: "Nexus Engine status" });
    await expect(status).toBeVisible();
    const panel = (await status.boundingBox())!;
    expect(panel.x).toBeGreaterThanOrEqual(0);
    expect(panel.x + panel.width).toBeLessThanOrEqual(390);
    expect(panel.y + panel.height).toBeLessThanOrEqual(frame.y + 1);
  });
});
