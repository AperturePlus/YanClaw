import './style.css';
import { recoverState, startAutoWatcher, startPolling } from './actions';
import { isAssistantBlockedHost } from './hostPolicy';
import { subscribe } from './state';
import { mountPanel, renderPanel } from './ui/panel';
import { mountToast } from './ui/toast';

if (!isAssistantBlockedHost()) {
  mountToast();
  mountPanel();
  subscribe(renderPanel);

  recoverState().then(() => {
    renderPanel();
    startPolling();
    startAutoWatcher();
  });
}
