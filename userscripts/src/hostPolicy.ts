const BLOCKED_HOSTS = new Set([
  'dx.scu.edu.cn',
  'mail.scu.edu.cn',
]);

export function isAssistantBlockedHost(hostname: string = window.location.hostname): boolean {
  return BLOCKED_HOSTS.has((hostname || '').toLowerCase());
}
