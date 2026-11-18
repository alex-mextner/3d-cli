
import '@webcomponents/scoped-custom-element-registry/scoped-custom-element-registry.min.js';
import { LitElement, html, css } from 'lit';
import { ContextProvider } from '@lit/context';
import { configContext, internationalizationContext } from "/__HA_FIXTURE_ROOT__/src/data/context/index.ts";
import { customCards as haCustomCards } from "/__HA_FIXTURE_ROOT__/src/data/lovelace_custom_cards.ts";
import "/__HA_FIXTURE_ROOT__/src/panels/lovelace/cards/hui-error-card.ts";
import { createLovelaceElement } from "/__HA_FIXTURE_ROOT__/src/panels/lovelace/create-element/create-element-base.ts";
window.__fixtureCatalog = haCustomCards;
class FixtureRoot extends LitElement {
  static styles = css`:host{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}slot{display:contents}`;
  constructor() {
    super();
    new ContextProvider(this, { context: configContext, initialValue: { user: { is_admin: true } } });
    new ContextProvider(this, { context: internationalizationContext, initialValue: {
      localize: key => key.endsWith('configuration_error') ? 'Configuration error' : 'Configuration warning'
    } });
  }
  render() { return html`<slot></slot>`; }
}
customElements.define('fixture-root', FixtureRoot);
customElements.define('home-assistant', class extends HTMLElement {});
await Promise.resolve(); await Promise.resolve();
const root = document.createElement('fixture-root');
document.querySelector('#cards').append(root);
const configs = await (await fetch('/configs.json')).json();
window.__fixtureApiCalls = [];
const hass = {
  states: {}, config: { external_url: 'https://fixture.invalid' },
  hassUrl: path => new URL(path, location.origin).href,
  callWS: async message => {
    window.__fixtureApiCalls.push(message.type);
    throw new Error('Isolated fixture: live API disabled');
  },
  callService: async () => { throw new Error('Isolated fixture: service actions disabled'); }
};
for (const config of configs) {
  const holder = document.createElement('div');
  const mount = () => {
    const card = createLovelaceElement('card', config);
    card.hass = hass;
    card.addEventListener('ll-rebuild', () => mount(), { once: true });
    holder.replaceChildren(card);
  };
  mount(); root.append(holder);
}
window.__fixtureMounted = true;
