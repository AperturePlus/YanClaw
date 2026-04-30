export function urlMatches(a: string, b: string): boolean {
  try {
    const u1 = new URL(a);
    const u2 = new URL(b);
    // Ignore protocol (http vs https) — many university sites redirect.
    return (
      u1.hostname === u2.hostname &&
      u1.pathname.replace(/\/+$/, '') === u2.pathname.replace(/\/+$/, '')
    );
  } catch {
    return false;
  }
}

export function truncUrl(url: string, max = 40): string {
  try {
    return new URL(url).pathname.slice(0, max);
  } catch {
    return url.slice(0, max);
  }
}

export function timeAgo(date: Date): string {
  const s = Math.round((Date.now() - date.getTime()) / 1000);
  if (s < 60) return `${s}s ago`;
  return `${Math.round(s / 60)}m ago`;
}

const STATUS_ICONS: Record<string, string> = {
  completed: '✅',
  skipped: '⏭',
  failed: '❌',
};

export function statusIcon(status: string): string {
  return STATUS_ICONS[status] ?? '❓';
}
