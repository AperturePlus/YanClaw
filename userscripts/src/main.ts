import './style.css';
import { recoverState, startAutoWatcher, startPolling } from './actions';
import { subscribe } from './state';
import { mountPanel, renderPanel } from './ui/panel';
import { mountToast } from './ui/toast';

mountToast();
mountPanel();
subscribe(renderPanel);

recoverState().then(() => {
  renderPanel();
  startPolling();
  startAutoWatcher();
});
