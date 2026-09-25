import { useEffect } from "react";
import type { SitePage } from "./routes";

/**
 * Per-route document metadata.
 *
 * index.html carries one static title and description, which was correct when
 * the site was one document. With six real routes, every one of them would
 * otherwise be indexed — and shared into Slack, iMessage or a tweet — as
 * "TradeLogX Nexus | Trading Intelligence Platform" with landing-page copy.
 * This rewrites the title, description, canonical URL and the Open Graph /
 * Twitter pair on navigation, and puts them back when the page unmounts so a
 * route never leaks its metadata into the next one.
 */

const BRAND = "TradeLogX Nexus";
const ORIGIN = "https://www.trade-logx.com";

function upsertMeta(selector: string, attr: "name" | "property", key: string, content: string) {
  let el = document.head.querySelector<HTMLMetaElement>(selector);
  if (!el) {
    el = document.createElement("meta");
    el.setAttribute(attr, key);
    document.head.appendChild(el);
  }
  const previous = el.getAttribute("content");
  el.setAttribute("content", content);
  return previous;
}

function upsertCanonical(href: string) {
  let el = document.head.querySelector<HTMLLinkElement>('link[rel="canonical"]');
  if (!el) {
    el = document.createElement("link");
    el.setAttribute("rel", "canonical");
    document.head.appendChild(el);
  }
  const previous = el.getAttribute("href");
  el.setAttribute("href", href);
  return previous;
}

/**
 * Breadcrumb structured data.
 *
 * Six sibling pages under one brand read to a crawler as six unrelated
 * documents unless the relationship is stated. This is also what produces the
 * "trade-logx.com › Engine" line in a result instead of a bare URL.
 */
function setBreadcrumb(label: string, url: string) {
  const id = "nx-breadcrumb-ld";
  document.getElementById(id)?.remove();
  const el = document.createElement("script");
  el.id = id;
  el.type = "application/ld+json";
  el.textContent = JSON.stringify({
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    itemListElement: [
      { "@type": "ListItem", position: 1, name: BRAND, item: `${ORIGIN}/` },
      { "@type": "ListItem", position: 2, name: label, item: url },
    ],
  });
  document.head.appendChild(el);
  return () => el.remove();
}

export interface PageMeta {
  title: string;
  description: string;
  /** Route path, e.g. "/engine". Used for the canonical + og:url. */
  path: string;
  /** Short name for the breadcrumb trail, e.g. "Engine". */
  label?: string;
  /** The page's base colour, mirrored into the mobile browser chrome. */
  themeColor?: string;
}

export function usePageMeta({ title, description, path, label, themeColor }: PageMeta) {
  useEffect(() => {
    const url = `${ORIGIN}${path}`;
    const fullTitle = `${title} | ${BRAND}`;

    const prevTitle = document.title;
    document.title = fullTitle;

    const restore: Array<() => void> = [];
    const set = (selector: string, attr: "name" | "property", key: string, value: string) => {
      const prev = upsertMeta(selector, attr, key, value);
      restore.push(() => {
        const el = document.head.querySelector<HTMLMetaElement>(selector);
        if (el && prev !== null) el.setAttribute("content", prev);
      });
    };

    set('meta[name="description"]', "name", "description", description);
    set('meta[property="og:title"]', "property", "og:title", fullTitle);
    set('meta[property="og:description"]', "property", "og:description", description);
    set('meta[property="og:url"]', "property", "og:url", url);
    set('meta[name="twitter:title"]', "name", "twitter:title", fullTitle);
    set('meta[name="twitter:description"]', "name", "twitter:description", description);
    if (themeColor) set('meta[name="theme-color"]', "name", "theme-color", themeColor);

    const prevCanonical = upsertCanonical(url);
    const clearBreadcrumb = label ? setBreadcrumb(label, url) : undefined;

    return () => {
      document.title = prevTitle;
      restore.forEach((fn) => fn());
      if (prevCanonical) upsertCanonical(prevCanonical);
      clearBreadcrumb?.();
    };
  }, [title, description, path, label, themeColor]);
}

/**
 * The usual case: take every piece of metadata straight from the route table.
 *
 * Pages called `usePageMeta` with three of the five fields spelled out by
 * hand, which meant `label` and `themeColor` had to be remembered separately
 * on each of six pages — exactly the kind of thing that is correct on the day
 * it is written and wrong two pages later.
 */
export function useRouteMeta(route: SitePage) {
  usePageMeta({
    title: route.title,
    description: route.description,
    path: route.path,
    label: route.label,
    themeColor: route.themeColor,
  });
}
