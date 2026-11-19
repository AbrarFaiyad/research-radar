async () => {
  // Reads the feed cards currently in the DOM. The LinkedIn feed is virtualized, so the
  // scanner calls this after every scroll step and dedupes by `key`.
  // Read-only except for expanding "… more" inside a post body, which changes nothing
  // on the account.
  const cards = [...document.querySelectorAll(
    'div[role="listitem"][componentkey^="update-card-focus"]'
  )];

  for (const c of cards) {
    const more = c.querySelector('[data-testid="expandable-text-button"]');
    if (more && more.offsetParent !== null) more.click();
  }
  if (cards.length) await new Promise((r) => setTimeout(r, 400));

  const unwrap = (href) => {
    try {
      const u = new URL(href);
      if (u.hostname.endsWith("linkedin.com") && u.pathname.startsWith("/safety/go")) {
        return u.searchParams.get("url") || href;
      }
      return href;
    } catch (e) {
      return href;
    }
  };
  const isInternal = (href) =>
    /linkedin\.com\/(in|company|school|feed|search|hashtag|showcase|groups|events|posts\/.*\/likes)/i.test(href) ||
    /media\.licdn\.com|static\.licdn\.com/i.test(href) ||
    href.startsWith("javascript:");

  const ageRe = /^(\d+)\s*(s|m|h|d|w|mo|y)\b/;

  return cards.map((c) => {
    const key = (c.getAttribute("componentkey") || "").replace("update-card-focus", "");
    const inner = c.innerText || "";
    const lines = inner.split("\n").map((s) => s.trim()).filter(Boolean);

    const menu = c.querySelector('button[aria-label^="Open control menu for post by "]');
    const author = menu
      ? menu.getAttribute("aria-label").replace("Open control menu for post by ", "").trim()
      : null;

    const profileLinks = [...c.querySelectorAll('a[href*="/in/"], a[href*="/company/"], a[href*="/school/"]')]
      .map((a) => a.href.split("?")[0]);
    const authorUrl = profileLinks[0] || null;
    let authorKind = "unknown";
    if (authorUrl) {
      if (authorUrl.includes("/in/")) authorKind = "person";
      else if (authorUrl.includes("/company/")) authorKind = "company";
      else if (authorUrl.includes("/school/")) authorKind = "school";
    }

    let degree = null;
    let headline = null;
    const degIdx = lines.findIndex((l) => /^•\s*(1st|2nd|3rd\+?)$/.test(l));
    if (degIdx >= 0) {
      degree = lines[degIdx].replace(/^•\s*/, "");
      const next = lines[degIdx + 1];
      if (next && !ageRe.test(next)) headline = next;
    }
    const ageLine = lines.find((l) => ageRe.test(l));
    const age = ageLine ? ageLine.split("•")[0].trim() : null;

    const body = c.querySelector('[data-testid="expandable-text-box"]');
    const text = (body ? body.innerText : "").replace(/…\s*more\s*$/, "").trim();

    const links = [...new Set(
      [...c.querySelectorAll("a[href]")]
        .map((a) => unwrap(a.href))
        .filter((h) => h && !isInternal(h))
    )].slice(0, 12);

    const promoted = lines.slice(0, 8).some((l) => l === "Promoted");

    return { key, author, authorUrl, authorKind, degree, headline, text, links, promoted, age };
  });
}
