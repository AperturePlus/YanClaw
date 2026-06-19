export function urlMatches(a: string, b: string): boolean {
  try {
    const u1 = new URL(a);
    const u2 = new URL(b);
    // Ignore protocol (http vs https) — many university sites redirect.
    return (
      u1.hostname === u2.hostname &&
      u1.pathname.replace(/\/+$/, '') === u2.pathname.replace(/\/+$/, '') &&
      normalizedSearch(u1) === normalizedSearch(u2)
    );
  } catch {
    return false;
  }
}

function normalizedSearch(url: URL): string {
  if (!url.search) return '';
  const params = [...url.searchParams.entries()].sort(([aKey, aValue], [bKey, bValue]) => {
    const keyOrder = aKey.localeCompare(bKey);
    return keyOrder || aValue.localeCompare(bValue);
  });
  return params.map(([key, value]) => `${key}=${value}`).join('&');
}

/** Match hostname only (ignoring protocol and path). Handles www prefix and http/https redirects. */
export function sameHost(a: string, b: string): boolean {
  try {
    const h1 = new URL(a).hostname.replace(/^www\./, '');
    const h2 = new URL(b).hostname.replace(/^www\./, '');
    return h1 === h2;
  } catch {
    return false;
  }
}

/** Loose check: same site root (e.g. both *.pku.edu.cn). */
export function sameSite(a: string, b: string): boolean {
  try {
    return siteRoot(new URL(a).hostname) === siteRoot(new URL(b).hostname);
  } catch {
    return false;
  }
}

function siteRoot(host: string): string {
  const parts = host.split('.');
  // Handle .edu.cn / .ac.cn style TLDs
  if (parts.length >= 3 && parts.at(-1) === 'cn' && ['edu', 'ac', 'com'].includes(parts.at(-2)!)) {
    return parts.slice(-3).join('.');
  }
  return parts.slice(-2).join('.');
}

export function truncUrl(url: string, max = 40): string {
  try {
    return new URL(url).pathname.slice(0, max);
  } catch {
    return url.slice(0, max);
  }
}

const ERROR_PATTERNS = /502 bad gateway|503 service|504 gateway|500 internal|error occurred|server error|nginx/i;

/** Detect if the current page is a server error page (502, 503, etc.). */
export function isErrorPage(): boolean {
  const title = document.title || '';
  const bodyText = document.body?.innerText || '';
  // Short page with error keywords = error page
  if (bodyText.length < 2000 && ERROR_PATTERNS.test(title + ' ' + bodyText)) return true;
  return false;
}
