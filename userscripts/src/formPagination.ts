import type { FetchAction, PaginationState } from './types';

const PAGE_ASSIGN_RE =
  /document\.forms\[['"]([^'"]+)['"]\]\.([A-Za-z0-9_]+)\.value\s*=\s*['"]?(\d+)['"]?/i;
const GOTO_FIELD_RE = /\b([A-Za-z0-9_]*?)GOPAGE\b/i;

export function collectFormPaginationStates(currentUrl: string = window.location.href): PaginationState[] {
  const anchors = [...document.querySelectorAll<HTMLAnchorElement>('a[href^="javascript:"]')];
  const byFormField = new Map<string, { formName: string; fieldName: string; pages: Set<number> }>();

  for (const anchor of anchors) {
    const parsed = parsePageAssignment(anchor.getAttribute('href') || '');
    if (!parsed) continue;
    const key = `${parsed.formName}\u0000${parsed.fieldName}`;
    const existing = byFormField.get(key) ?? {
      formName: parsed.formName,
      fieldName: parsed.fieldName,
      pages: new Set<number>(),
    };
    existing.pages.add(parsed.pageIndex);
    byFormField.set(key, existing);
  }

  const currentPage = detectCurrentPage(currentUrl);
  const states: PaginationState[] = [];
  const seen = new Set<string>();
  for (const item of byFormField.values()) {
    const pageIndexes = expandPageIndexes(item.formName, item.fieldName, item.pages);
    const totalPages = Math.max(...pageIndexes, ...item.pages);
    for (const pageIndex of pageIndexes) {
      if (pageIndex <= 1 || pageIndex === currentPage) continue;
      const syntheticUrl = buildSyntheticUrl(currentUrl, item.formName, item.fieldName, pageIndex);
      if (seen.has(syntheticUrl)) continue;
      seen.add(syntheticUrl);
      states.push({
        kind: 'form_submit',
        state_id: `form:${item.formName}:${item.fieldName}:${pageIndex}`,
        label: `${item.formName} 第 ${pageIndex} 页`,
        page_index: pageIndex,
        total_pages: totalPages,
        form_name: item.formName,
        fields: { [item.fieldName]: String(pageIndex) },
        submit: true,
        synthetic_url: syntheticUrl,
        url: currentUrl,
      });
    }
  }

  return states.sort((a, b) => a.page_index - b.page_index || a.synthetic_url.localeCompare(b.synthetic_url));
}

export function actionMatchesCurrentPage(action: FetchAction | null | undefined, currentUrl: string, targetUrl: string): boolean {
  if (!action || action.kind !== 'form_submit') return true;
  const desired = Number(action.page_index || firstFieldValue(action.fields));
  const current = detectCurrentPage(currentUrl);
  if (!desired || !current) return false;
  return desired === current && sameBaseUrl(currentUrl, targetUrl);
}

export function performFetchAction(action: FetchAction | null | undefined): boolean {
  if (!action || action.kind !== 'form_submit') return false;
  const formName = action.form_name || '';
  const form = document.forms.namedItem(formName);
  if (!form) return false;
  for (const [name, value] of Object.entries(action.fields ?? {})) {
    const control = form.elements.namedItem(name);
    if (!control) continue;
    setControlValue(control, value);
  }
  if (action.submit !== false) {
    form.submit();
  }
  return true;
}

function parsePageAssignment(href: string): { formName: string; fieldName: string; pageIndex: number } | null {
  const match = PAGE_ASSIGN_RE.exec(href);
  if (!match) return null;
  const pageIndex = Number(match[3]);
  if (!Number.isFinite(pageIndex) || pageIndex <= 0) return null;
  return { formName: match[1], fieldName: match[2], pageIndex };
}

function expandPageIndexes(formName: string, fieldName: string, pages: Set<number>): number[] {
  const hasGoto = [...document.querySelectorAll<HTMLInputElement>('input[name]')].some((input) => {
    const name = input.name || '';
    const match = GOTO_FIELD_RE.exec(name);
    if (!match) return false;
    const prefix = match[1] || '';
    const form = input.form;
    return (!form || form.name === formName) && (!prefix || fieldName.toLowerCase().startsWith(prefix.toLowerCase()));
  });
  if (!hasGoto) return [...pages].sort((a, b) => a - b);
  const maxPage = Math.max(...pages);
  return Array.from({ length: maxPage }, (_unused, index) => index + 1);
}

function detectCurrentPage(currentUrl: string): number {
  const current = Number(document.querySelector('.this-page')?.textContent?.trim() || '0');
  if (Number.isFinite(current) && current > 0) return current;
  try {
    const url = new URL(currentUrl);
    for (const key of ['PAGENUM', 'page', 'p', 'pn', 'fromWenNOWPAGE']) {
      const value = Number(url.searchParams.get(key) || '0');
      if (Number.isFinite(value) && value > 0) return value;
    }
  } catch {
    // ignore
  }
  return 1;
}

function buildSyntheticUrl(url: string, formName: string, fieldName: string, pageIndex: number): string {
  const parsed = new URL(url);
  for (const key of [...parsed.searchParams.keys()]) {
    if (key.startsWith('__ycl_')) parsed.searchParams.delete(key);
  }
  parsed.searchParams.set('__ycl_kind', 'form');
  parsed.searchParams.set('__ycl_form', formName);
  parsed.searchParams.set('__ycl_field', fieldName);
  parsed.searchParams.set('__ycl_page', String(pageIndex));
  parsed.hash = '';
  return parsed.toString();
}

function setControlValue(control: Element | RadioNodeList, value: string): void {
  if (control instanceof RadioNodeList) {
    control.value = value;
    return;
  }
  if ('value' in control) {
    (control as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement).value = value;
  }
}

function firstFieldValue(fields?: Record<string, string>): string {
  return Object.values(fields ?? {})[0] ?? '';
}

function sameBaseUrl(a: string, b: string): boolean {
  try {
    const aUrl = new URL(a);
    const bUrl = new URL(b);
    return aUrl.hostname === bUrl.hostname && stripSlash(aUrl.pathname) === stripSlash(bUrl.pathname);
  } catch {
    return false;
  }
}

function stripSlash(value: string): string {
  return value.replace(/\/+$/, '');
}
