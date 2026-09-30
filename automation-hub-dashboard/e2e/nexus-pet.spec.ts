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
  const productionImage = productionSprite.locator(".nexus-pet-pose-working .nexus-pet-production-image");
  await expect(productionSprite).toBeVisible();
  await expect(productionImage).toHaveAttribute("src", /nexus-pet-concepts\/sprig-body-v5\.png$/);
  await expect(productionSprite.locator(".nexus-pet-pose-working .nexus-pet-sprout-image"))
    .toHaveAttribute("src", /nexus-pet-concepts\/sprig-sprout-v5\.png$/);
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
  for (const layer of ["production-image", "sprout-image"]) {
    const image = sprig.locator(`.nexus-pet-pose-working .nexus-pet-${layer}`);
    await expect.poll(() => image.evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth)).toBe(2480);
    // The artwork (body and sprout layer alike): four equal cells whose edge columns are empty.
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
      expect(left, layer).toBeLessThanOrEqual(16);
      expect(right, layer).toBeLessThanOrEqual(16);
    }
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
  for (const selector of [".nexus-pet-button", ".nexus-pet-stage", ".nexus-pet-production-sprite", ".nexus-pet-pose",
    ".nexus-pet-production-image", ".nexus-pet-sprout", ".nexus-pet-sprout-image", ".nexus-pet-screen-glow"]) {
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
// Where an eye looks: its LED's centre relative to its socket, in px (right and down are positive).
const eyeOffset = (page: Page, pose: string) => page.locator(`.nexus-pet-pose-${pose} .nexus-pet-socket-l`).evaluate((socket) => {
  const s = socket.getBoundingClientRect();
  const l = socket.querySelector(".nexus-pet-led")!.getBoundingClientRect();
  return { x: l.x + l.width / 2 - (s.x + s.width / 2), y: l.y + l.height / 2 - (s.y + s.height / 2) };
});
const ledX = async (page: Page) => (await eyeOffset(page, "aware")).x;
const layerOpacity = (page: Page, pose: string) => page.locator(`.nexus-pet-pose-${pose}`)
  .evaluate((el) => getComputedStyle(el).opacity);

test("Sprig notices the pointer, looks at it, and waves when touched", async ({ page }) => {
  const { root, button, cx, cy } = await openSprig(page, snapshot("running"));
  await expect(root).toHaveAttribute("data-state", "running");
  await page.mouse.move(5, 5);
  await expect(root).toHaveAttribute("data-pose", "working");
  await expect(root).toHaveAttribute("data-eyes", "open");

  await page.mouse.move(cx - 120, cy, { steps: 4 });          // near, to the left: looks up and left
  await expect(root).toHaveAttribute("data-pose", "aware");
  await expect.poll(() => ledX(page)).toBeLessThan(-.25);
  await page.mouse.move(cx + 110, cy - 20, { steps: 6 });     // to the right: the eyes follow
  await expect.poll(() => ledX(page)).toBeGreaterThan(.25);

  await button.hover();                                        // on Sprig: waves with its painted happy eyes
  await expect(root).toHaveAttribute("data-pose", "greet");
  await expect(page.locator(".nexus-pet-pose-greet .nexus-pet-live-eyes")).toHaveCount(0);
  await expect.poll(() => layerOpacity(page, "greet")).toBe("1");  // the greeting fades in over the last pose
  await expect.poll(() => layerOpacity(page, "aware")).toBe("0");
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
  // asleep, but never see-through; the laptop's screen sleeps too
  expect(await page.locator(".nexus-pet-stage").evaluate((el) => getComputedStyle(el).opacity)).toBe("1");
  expect(await page.locator(".nexus-pet-pose-working .nexus-pet-screen-glow")
    .evaluate((el) => getComputedStyle(el).backgroundImage)).toContain("rgba(40, 42, 46");
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

test("the sprout is cut from the same artwork: never over the body, only above the head", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/overview");
  const layer = (name: string) => page.locator(`.nexus-pet-pose-working .nexus-pet-${name}`);
  for (const name of ["production-image", "sprout-image"]) {
    await expect.poll(() => layer(name).evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth)).toBe(2480);
  }
  const report = await page.evaluate(() => {
    const pixels = (selector: string) => {
      const img = document.querySelector<HTMLImageElement>(selector)!;
      const canvas = document.createElement("canvas");
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      const ctx = canvas.getContext("2d")!;
      ctx.drawImage(img, 0, 0);
      return ctx.getImageData(0, 0, canvas.width, canvas.height);
    };
    const body = pixels(".nexus-pet-pose-working .nexus-pet-production-image");
    const sprout = pixels(".nexus-pet-pose-working .nexus-pet-sprout-image");
    let overlap = 0;
    const cells = [0, 1, 2, 3].map(() => ({ count: 0, lowest: 0 }));
    for (let i = 3; i < sprout.data.length; i += 4) {
      if (!sprout.data[i]) continue;
      if (body.data[i]) overlap += 1;
      const p = (i - 3) / 4;
      const cell = cells[Math.floor((p % sprout.width) / (sprout.width / 4))];
      cell.count += 1;
      cell.lowest = Math.max(cell.lowest, Math.floor(p / sprout.width) / sprout.height);
    }
    return { overlap, cells };
  });
  expect(report.overlap).toBe(0);                                  // every pixel is in exactly one layer
  for (const cell of report.cells) {
    expect(cell.count).toBeGreaterThan(2000);                      // each pose has its sprout
    expect(cell.lowest).toBeLessThan(.3);                          // and nothing below the stem's base
  }
});

test("Sprig's sprout swings on its stem when Sprig hops, then settles", async ({ page }) => {
  const { cx, cy } = await openSprig(page, snapshot("running"));
  await page.mouse.move(cx, cy, { steps: 3 });                     // greet first, and let the sprout settle
  await page.waitForTimeout(2500);
  await page.evaluate(() => {
    const w = window as unknown as { swing: number[] };
    w.swing = [];
    const sprout = document.querySelector(".nexus-pet-pose-greet .nexus-pet-sprout")!;
    const t0 = performance.now();
    const sample = () => {
      w.swing.push(parseFloat(getComputedStyle(sprout).rotate) || 0);  // the rendered turn, in degrees
      if (performance.now() - t0 < 3200) requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
  });
  await page.mouse.click(cx, cy);                                  // the hop throws the sprout
  await page.waitForTimeout(3400);
  const swing = await page.evaluate(() => (window as unknown as { swing: number[] }).swing);
  const peak = Math.max(...swing.map(Math.abs));
  const big = swing.filter((a) => Math.abs(a) > .2);
  const reversals = big.slice(1).filter((a, i) => Math.sign(a) !== Math.sign(big[i])).length;
  expect(peak).toBeGreaterThan(4);                                 // a visible swing
  expect(peak).toBeLessThanOrEqual(18);                            // never torn off its stem
  expect(reversals).toBeGreaterThanOrEqual(2);                     // overshoots and swings back, like a stem
  expect(Math.abs(swing[swing.length - 1])).toBeLessThan(.3);      // and comes to rest
});

test("Sprig watches its laptop while working, and its eyes sweep the screen while analysing", async ({ page }) => {
  const { root } = await openSprig(page, snapshot("running"));
  await page.mouse.move(5, 5);
  await expect(root).toHaveAttribute("data-pose", "working");
  // looking down and left, at the screen, whatever small eye movement is under way
  await expect.poll(async () => { const e = await eyeOffset(page, "working"); return e.x < -.3 && e.y > .2; }).toBe(true);

  const warming = snapshot("running");
  warming.instances[0].state = "warming";
  await page.route((url) => url.pathname === "/instances", (route) => route.fulfill({ json: warming }));
  await page.reload();
  await expect(root).toHaveAttribute("data-state", "analysing");
  await page.mouse.move(5, 5);
  await expect(root).toHaveAttribute("data-eyes", "scan");
  await expect.poll(() => page.locator(".nexus-pet-pose-working .nexus-pet-led").first()
    .evaluate((el) => getComputedStyle(el).animationName)).toBe("nexus-pet-led-scan");
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
  const box = (await page.getByRole("button", { name: /Sprig, Nexus pet:/ }).boundingBox())!;
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await page.waitForTimeout(300);
  expect(await page.locator(".nexus-pet-pose-greet .nexus-pet-sprout")
    .evaluate((el) => parseFloat(getComputedStyle(el).rotate) || 0)).toBe(0);   // no swing, no breeze
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
