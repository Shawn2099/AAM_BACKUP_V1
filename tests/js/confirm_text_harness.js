// Runs static/js/dashboard.js in a stubbed browser and prints the text of the confirm() a LAN backup button shows.
// usage: node confirm_text_harness.js <dashboard.js> <classic|fy> <wol yes|no> <shutdown yes|no>
const fs = require('fs'), vm = require('vm');
const [file, kind, wol, shutdown] = process.argv.slice(2);
const els = {};
function el(id) {
  return els[id] || (els[id] = { id, dataset: {}, style: {}, innerText: '', attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; },
    appendChild() {}, classList: { add() {}, remove() {} }, remove() {}, textContent: '' });
}
let shown = null;
const ctx = {
  document: { getElementById: el, createElement: () => el('t'), addEventListener() {}, querySelector: () => null,
              querySelectorAll: () => [] },
  window: { addEventListener() {} }, console, setTimeout: () => 0, setInterval: () => 0,
  confirm: (m) => { shown = m; return false; },       // cancel: nothing is sent
  fetch: async () => { throw new Error('must not be called'); },
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(file, 'utf8'), ctx);
// the settings live on the classic button and on the per-FY container
el('btn-lan').dataset.wol = wol; el('btn-lan').dataset.shutdown = shutdown;
el('fy-cards').dataset.wol = wol; el('fy-cards').dataset.shutdown = shutdown;
(async () => {
  if (kind === 'classic') await ctx.triggerBackup({ preventDefault() {} }, 'lan');
  else await ctx.triggerFyBackup('lan', 'FY27-28', el('b'));
  console.log(JSON.stringify({ confirm: shown }));
})();
