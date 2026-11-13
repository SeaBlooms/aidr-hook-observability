import { load, save } from "../prefs.js";
import { Component, Fragment, html } from "../html.js";

function cellText(value) {
  if (value == null) return "";
  if (typeof value === "string" || typeof value === "number") return String(value);
  return "";
}

function csvEscape(value) {
  const text = value == null ? "" : String(value);
  if (/[",\n]/.test(text)) return '"' + text.replace(/"/g, '""') + '"';
  return text;
}

function download(filename, text, type) {
  const blob = new Blob([text], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export class DataTable extends Component {
  constructor(props) {
    super(props);
    const hidden = (props.tableId && load().columns[props.tableId]) || [];
    this.state = {
      sort: props.initialSort || null,
      dir: props.initialDir || "desc",
      expanded: {},
      detailOpen: {},
      active: 0,
      hidden,
      picking: false,
    };
  }

  visibleColumns() {
    const hidden = new Set(this.state.hidden);
    return (this.props.columns || []).filter((column) => !hidden.has(column.id));
  }

  sortedRows() {
    const rows = (this.props.rows || []).slice();
    const column = (this.props.columns || []).find((item) => item.id === this.state.sort);
    if (!column) return rows;
    const dir = this.state.dir === "asc" ? 1 : -1;
    rows.sort((a, b) => {
      const left = column.sortValue ? column.sortValue(a) : a[column.id];
      const right = column.sortValue ? column.sortValue(b) : b[column.id];
      if (left == null && right == null) return 0;
      if (left == null) return 1;
      if (right == null) return -1;
      if (typeof left === "number" && typeof right === "number") return (left - right) * dir;
      return String(left).localeCompare(String(right)) * dir;
    });
    return rows;
  }

  toggleSort(id) {
    if (this.state.sort === id) this.setState({ dir: this.state.dir === "asc" ? "desc" : "asc" });
    else this.setState({ sort: id, dir: "asc" });
  }

  toggleColumn(id) {
    const hidden = new Set(this.state.hidden);
    if (hidden.has(id)) hidden.delete(id);
    else hidden.add(id);
    const next = [...hidden];
    this.setState({ hidden: next });
    if (this.props.tableId) {
      const columns = { ...load().columns, [this.props.tableId]: next };
      save({ columns });
    }
  }

  exportAs(kind) {
    const columns = this.visibleColumns();
    const rows = this.sortedRows();
    const name = (this.props.exportName || "aidr-obs") + "." + kind;
    if (kind === "json") {
      const data = rows.map((row) => {
        const out = {};
        columns.forEach((column) => {
          out[column.id] = column.exportValue ? column.exportValue(row) : column.sortValue ? column.sortValue(row) : row[column.id];
        });
        return out;
      });
      download(name, JSON.stringify(data, null, 2), "application/json");
      return;
    }
    const header = columns.map((column) => csvEscape(column.label)).join(",");
    const body = rows
      .map((row) =>
        columns
          .map((column) => csvEscape(column.exportValue ? column.exportValue(row) : column.sortValue ? column.sortValue(row) : cellText(row[column.id])))
          .join(","),
      )
      .join("\n");
    download(name, header + "\n" + body, "text/csv");
  }

  onKey(event) {
    const rows = this.sortedRows();
    if (!rows.length) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      const active = Math.max(0, Math.min(rows.length - 1, this.state.active + step));
      this.setState({ active });
      const node = event.currentTarget.querySelector('[data-row="' + active + '"]');
      if (node) node.focus();
    } else if (event.key === "Enter") {
      const row = rows[this.state.active];
      if (row && this.props.onRow) this.props.onRow(row);
    }
  }

  render() {
    const columns = this.visibleColumns();
    const rows = this.sortedRows();
    const caption = this.props.caption || "Table";
    return html`<div class="table-block">
      <div class="table-tools">
        <span class="muted">${this.props.meta || rows.length + " rows"}</span>
        <span class="spacer"></span>
        ${this.props.tableId
          ? html`<button class="btn mini" type="button" aria-expanded=${this.state.picking ? "true" : "false"} onClick=${() => this.setState({ picking: !this.state.picking })}>Columns</button>`
          : null}
        <button class="btn mini" type="button" onClick=${() => this.exportAs("csv")}>CSV</button>
        <button class="btn mini" type="button" onClick=${() => this.exportAs("json")}>JSON</button>
      </div>
      ${this.state.picking
        ? html`<div class="col-picker" role="group" aria-label="Columns">
            ${(this.props.columns || []).map(
              (column) => html`<label key=${column.id}><input type="checkbox" checked=${!this.state.hidden.includes(column.id)} onChange=${() => this.toggleColumn(column.id)} /> ${column.label}</label>`,
            )}
          </div>`
        : null}
      ${rows.length
        ? html`<div class="table-wrap" onKeyDown=${(event) => this.onKey(event)}>
            <table>
              <caption class="sr-only">${caption}</caption>
              <thead><tr>
                ${this.props.detail ? html`<th scope="col"><span class="sr-only">Events</span></th>` : null}
                ${columns.map((column) => html`<th key=${column.id} scope="col" aria-sort=${this.state.sort === column.id ? (this.state.dir === "asc" ? "ascending" : "descending") : "none"}>
                  <button class="th" type="button" onClick=${() => this.toggleSort(column.id)}>${column.label}</button>
                </th>`)}
              </tr></thead>
              <tbody>
                ${rows.map((row, index) => {
                  const key = this.props.rowKey ? this.props.rowKey(row) : index;
                  const open = !!this.state.detailOpen[key];
                  return html`<${Fragment} key=${key}>
                    <tr
                      data-row=${index}
                      tabindex=${index === this.state.active ? "0" : "-1"}
                      class=${this.props.onRow ? "clickable" : ""}
                      aria-selected=${index === this.state.active ? "true" : "false"}
                      onClick=${() => {
                        this.setState({ active: index });
                        if (this.props.onRow) this.props.onRow(row);
                      }}
                      onFocus=${() => this.setState({ active: index })}>
                      ${this.props.detail
                        ? html`<td><button class="btn mini" type="button" aria-expanded=${open ? "true" : "false"} aria-label="Hook events"
                            onClick=${(event) => {
                              event.stopPropagation();
                              this.setState({ detailOpen: { ...this.state.detailOpen, [key]: !open } });
                            }}>${open ? "Hide" : "Events"}</button></td>`
                        : null}
                      ${columns.map((column) => this.cell(column, row, index))}
                    </tr>
                    ${open
                      ? html`<tr class="detail"><td colspan=${columns.length + 1}>${this.props.detail(row)}</td></tr>`
                      : null}
                  </${Fragment}>`;
                })}
              </tbody>
            </table>
          </div>`
        : html`<div class="empty">${this.props.empty || "Nothing in this range"}</div>`}
    </div>`;
  }

  cell(column, row, index) {
    const key = column.id + ":" + index;
    const expanded = !!this.state.expanded[key];
    const content = column.render ? column.render(row) : row[column.id];
    if (!column.truncate) return html`<td key=${column.id}>${content}</td>`;
    const full = column.exportValue ? column.exportValue(row) : column.sortValue ? column.sortValue(row) : cellText(row[column.id]);
    const long = String(full || "").length > 80;
    return html`<td key=${column.id} class=${expanded ? "expanded" : "clip-cell"}>
      ${expanded ? html`<span class="full">${full}</span>` : content}
      ${long
        ? html`<button class="btn mini" type="button" onClick=${(event) => {
            event.stopPropagation();
            this.setState({ expanded: { ...this.state.expanded, [key]: !expanded } });
          }}>${expanded ? "Less" : "More"}</button>`
        : null}
    </td>`;
  }
}
