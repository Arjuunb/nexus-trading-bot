import { expect, test } from "@playwright/test";
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
