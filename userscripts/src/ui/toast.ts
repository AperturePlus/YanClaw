let toastEl: HTMLDivElement | null = null;
let hideTimer: ReturnType<typeof setTimeout> | null = null;

export function mountToast(): void {
  toastEl = document.createElement('div');
  toastEl.id = 'ycl-toast';
  document.body.appendChild(toastEl);
}

export function showToast(msg: string, duration = 3000): void {
  if (!toastEl) return;
  toastEl.textContent = msg;
  toastEl.style.display = 'block';
  if (hideTimer) clearTimeout(hideTimer);
  hideTimer = setTimeout(() => {
    if (toastEl) toastEl.style.display = 'none';
  }, duration);
}
