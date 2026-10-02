import { setReact } from '@ervisio/plugin-sdk/react';
import { setSdk, type PluginSDK } from './sdk';
import { registerStrings } from './i18n';
import { App } from './app';
import { ServersWidget } from './widget';
import styleText from './styles.css?inline';

export default function activate(sdk: PluginSDK): void {
  setSdk(sdk);
  setReact(sdk.react);
  registerStrings();
  if (!document.getElementById('mc-plugin-styles')) {
    const style = document.createElement('style');
    style.id = 'mc-plugin-styles';
    style.textContent = styleText;
    document.head.appendChild(style);
  }
  sdk.registerPage('minecraft', App);
  sdk.registerWidget({ id: 'servers', render: ServersWidget });
}
