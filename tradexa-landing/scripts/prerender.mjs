import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import path from "node:path";

const root = process.cwd();
const dist = path.join(root, "dist");
const serverBuild = path.join(root, "dist-ssr", "entry-server.js");
const { fullTitle, render, seoRoutes } = await import(pathToFileURL(serverBuild).href);
const template = await readFile(path.join(dist, "index.html"), "utf8");
const origin = "https://www.trade-logx.com";

function escapeAttribute(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function replaceMeta(html, selector, content) {
  const pattern = new RegExp(`<meta([^>]*${selector}[^>]*)>`, "i");
  return html.replace(pattern, (tag) => {
    const escaped = escapeAttribute(content);
    return /content="[^"]*"/i.test(tag)
      ? tag.replace(/content="[^"]*"/i, `content="${escaped}"`)
      : tag.replace(/\s*\/?\s*>$/, ` content="${escaped}" />`);
  });
}

function assertSingleMatch(html, pattern, expected, message) {
  const matches = [...html.matchAll(pattern)];
  if (matches.length !== 1 || matches[0][1] !== expected) {
    const actual = matches.length === 1 ? matches[0][1] : `${matches.length} matches`;
    throw new Error(`${message}; expected ${expected}, got ${actual}`);
  }
}

function documentFor(route) {
  if (route.description.length < 70 || route.description.length > 160) {
    throw new Error(`${route.path} description must be 70-160 characters; got ${route.description.length}`);
  }
  const url = `${origin}${route.path}`;
  const title = fullTitle(route);
  const rendered = render(route.path);
  const h1Count = (rendered.match(/<h1\b/gi) ?? []).length;
  const linkCount = (rendered.match(/<a\b/gi) ?? []).length;
  const wordCount = rendered
    .replace(/<[^>]+>/g, " ")
    .replace(/&[^;]+;/g, " ")
    .trim()
    .split(/\s+/).length;
  if (h1Count !== 1) throw new Error(`${route.path} must pre-render exactly one h1; got ${h1Count}`);
  if (linkCount < 2) throw new Error(`${route.path} must pre-render outgoing links; got ${linkCount}`);
  if (wordCount < 200) throw new Error(`${route.path} pre-render is thin; got ${wordCount} words`);
  let html = template
    .replace(/<title>[\s\S]*?<\/title>/i, `<title>${escapeAttribute(title)}</title>`)
    .replace(/<link rel="canonical" href="[^"]*"\s*\/>/i, `<link rel="canonical" href="${url}" />`)
    .replace(
      /<!-- nx-prerender:start -->[\s\S]*?<!-- nx-prerender:end -->/,
      `<!-- nx-prerender:start -->${rendered}<!-- nx-prerender:end -->`,
    );
  html = replaceMeta(html, 'name="description"', route.description);
  html = replaceMeta(html, 'name="theme-color"', route.themeColor);
  html = replaceMeta(html, 'property="og:title"', title);
  html = replaceMeta(html, 'property="og:description"', route.description);
  html = replaceMeta(html, 'property="og:url"', url);
  html = replaceMeta(html, 'name="twitter:title"', title);
  html = replaceMeta(html, 'name="twitter:description"', route.description);
  if (route.path !== "/") {
    const breadcrumb = JSON.stringify({
      "@context": "https://schema.org",
      "@type": "BreadcrumbList",
      itemListElement: [
        { "@type": "ListItem", position: 1, name: "TradeLogX Nexus", item: `${origin}/` },
        { "@type": "ListItem", position: 2, name: route.label, item: url },
      ],
    }).replaceAll("<", "\\u003c");
    html = html.replace("</head>", `<script type="application/ld+json">${breadcrumb}</script>\n  </head>`);
  }
  assertSingleMatch(
    html,
    /<link rel="canonical" href="([^"]+)"\s*\/>/gi,
    url,
    `${route.path} must have one self-referencing canonical`,
  );
  assertSingleMatch(
    html,
    /<meta property="og:url" content="([^"]+)"\s*\/>/gi,
    url,
    `${route.path} must have one matching og:url`,
  );
  return html;
}

const seoDir = path.join(dist, "seo");
await rm(seoDir, { recursive: true, force: true });
await mkdir(seoDir, { recursive: true });
for (const route of seoRoutes) {
  const filename = route.path === "/" ? "index.html" : `${route.path.slice(1)}.html`;
  await writeFile(path.join(seoDir, filename), documentFor(route));
}
await rm(path.join(root, "dist-ssr"), { recursive: true, force: true });
console.log(`Pre-rendered ${seoRoutes.length} public routes.`);
