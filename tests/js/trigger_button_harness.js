// Runs static/js/dashboard.js in a stubbed browser (no DOM library) and drives triggerBackup().
// usage: node trigger_button_harness.js <dashboard.js> <ok|fail|throw>   prints the button state as JSON
const fs = require('fs'), vm = require('vm');
const [file, scenario] = process.argv.slice(2);
const els = {};
function el(id) {
  return els[id] || (els[id] = { id, dataset: {}, style: {}, innerText: 'Run Cloud Backup', attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; },
    appendChild() {}, classList: { add() {}, remove() {} }, remove() {}, textContent: '' });
}
const toasts = [];
const ctx = {
  document: { getElementById: el, createElement: () => el('t' + toasts.length), addEventListener() {},
              querySelector: () => null, querySelectorAll: () => [] },
  window: { addEventListener() {} }, confirm: () => true, console, setTimeout: () => 0, setInterval: () => 0,
  fetch: async () => {
    if (scenario === 'throw') throw new Error('offline');
    return { ok: scenario === 'ok', json: async () => ({ detail: scenario === 'ok' ? 'The Cloud backup completed.' : 'It failed.' }) };
  },
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(file, 'utf8'), ctx);
ctx.showToast = (m, t) => toasts.push([t, m]);
ctx.updateStatus = async () => {};           // a finished run is "not running": the status poll changes nothing
(async () => {
  const btn = el('btn-cloud');
  await ctx.triggerBackup({ preventDefault() {} }, 'cloud');
  console.log(JSON.stringify({ text: btn.innerText, pointer: btn.style.pointerEvents, opacity: btn.style.opacity,
                               disabled: 'disabled' in btn.attrs, toasts }));
})();
