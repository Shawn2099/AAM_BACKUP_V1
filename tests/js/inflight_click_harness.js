// Runs static/js/dashboard.js in a stubbed browser and clicks a start button twice while the first request is
// still in flight. usage: node inflight_click_harness.js <dashboard.js> <classic|fy|verify>
// prints {fetches, disabledDuringFlight, textDuringFlight, disabledAfter} as JSON
const fs = require('fs'), vm = require('vm');
const [file, scenario] = process.argv.slice(2);
const els = {};
function el(id) {
  return els[id] || (els[id] = { id, dataset: {}, style: {}, innerText: 'Run Cloud Backup', attrs: {}, disabled: false,
    setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; },
    appendChild() {}, classList: { add() {}, remove() {} }, remove() {}, textContent: '' });
}
let fetches = 0, release;
const gate = new Promise(r => { release = r; });
const ctx = {
  document: { getElementById: el, createElement: () => el('t'), addEventListener() {},
              querySelector: () => null, querySelectorAll: () => [] },
  window: { addEventListener() {} }, confirm: () => true, console, setTimeout: () => 0, setInterval: () => 0,
  fetch: async () => { fetches++; await gate; return { ok: true, status: 200, json: async () => ({ detail: 'started' }) }; },
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(file, 'utf8'), ctx);
ctx.showToast = () => {};
ctx.updateStatus = async () => {};
const ev = { preventDefault() {} };
const btn = el(scenario === 'verify' ? 'btn-verify-lan' : 'btn-cloud');
const click = () => scenario === 'classic' ? ctx.triggerBackup(ev, 'cloud')
  : scenario === 'verify' ? ctx.triggerVerify('lan')
  : ctx.triggerFyBackup('cloud', 'FY26-27', btn);
(async () => {
  const first = click();
  await new Promise(r => setImmediate(r));          // the first request is now in flight
  const second = click();                           // second click: must send nothing
  await new Promise(r => setImmediate(r));
  const during = { disabled: 'disabled' in btn.attrs || btn.disabled === true, text: btn.innerText, fetches };
  release();
  await Promise.all([first, second]);
  console.log(JSON.stringify({ fetches, disabledDuringFlight: during.disabled, fetchesDuringFlight: during.fetches,
                               textDuringFlight: during.text }));
})();
