import { Drawer } from "./components/drawer.js";
import { Palette } from "./components/search.js";
import { Help, Shell, installKeys } from "./components/shell.js";
import { Fragment, Component, html, render } from "./html.js";
import { boot, get, subscribe } from "./state.js";
import { Activity } from "./views/activity.js";
import { Destinations } from "./views/destinations.js";
import { GraphPage } from "./views/graph.js";
import { Health } from "./views/health.js";
import { Mcp } from "./views/mcp.js";
import { Overview } from "./views/overview.js";
import { Sensitive } from "./views/sensitive.js";
import { SessionPage } from "./views/session.js";
import { Sessions } from "./views/sessions.js";
import { Usage } from "./views/usage.js";
import { Verdicts } from "./views/verdicts.js";

const VIEWS = {
  overview: Overview,
  activity: Activity,
  sessions: Sessions,
  session: SessionPage,
  graph: GraphPage,
  verdicts: Verdicts,
  sensitive: Sensitive,
  destinations: Destinations,
  health: Health,
  mcp: Mcp,
  usage: Usage,
};

class App extends Component {
  componentDidMount() {
    this.unsub = subscribe(() => this.forceUpdate());
  }

  componentWillUnmount() {
    if (this.unsub) this.unsub();
  }

  componentDidCatch(error) {
    this.setState({ crash: error && error.message ? error.message : String(error) });
  }

  render() {
    if (this.state && this.state.crash) {
      return html`<main id="main" data-state="error"><div class="card error-card"><p>${this.state.crash}</p></div></main>`;
    }
    const app = get();
    const View = VIEWS[app.view] || Overview;
    return html`<${Fragment}>
      <${Shell} app=${app}>
        <${View} app=${app} key=${app.view + ":" + (app.sessionKey || "")} />
      <//>
      <${Drawer} app=${app} />
      <${Palette} app=${app} />
      <${Help} app=${app} />
    <//>`;
  }
}

boot();
installKeys();
render(html`<${App} />`, document.getElementById("app"));
