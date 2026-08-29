// Typing a module ID must survive the status poll that rebuilds this list.
// Before this was fixed, typing an ID and pausing replaced it with the
// auto-suggested one — 0, when nothing was assigned yet.
const fs = require('fs');
const { El } = require(require('path').join(__dirname, 'dom_stub.js'));

const src = fs.readFileSync(require('path').join(__dirname, '..', '..', 'server', 'static', 'app.js'), 'utf8');
const grab = name => {
  const i = src.indexOf(`function ${name}(`);
  let depth = 0, started = false;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') { depth++; started = true; }
    else if (src[j] === '}') { depth--; if (started && depth === 0) return src.slice(i, j + 1); }
  }
};
const list = new El();
const ctx = { document: { getElementById: () => list, activeElement: null }, escapeProvision: s => s };
const code = [grab('collectTypedIds'), grab('restoreTypedIds'), grab('renderUnprovisionedModules')].join('\n');
const fn = new Function('document', 'escapeProvision', code + '; return renderUnprovisionedModules;');
const render = fn(ctx.document, ctx.escapeProvision);

const items = [{ serial: 'AAAA', age_seconds: 3 }, { serial: 'BBBB', age_seconds: 4 }];
let failures = 0;
const check = (label, got, want) => {
  const ok = got === want;
  if (!ok) failures++;
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}  (got ${JSON.stringify(got)}, want ${JSON.stringify(want)})`);
};

render(items, [], 0);
check('first render suggests an id', list.children[0].value, '0');

// the reported bug: type an id, then let the 2.5s poll fire
list.children[0].value = '17';
list.children[0].dataset.edited = '1';
render(items, [], 0);
check('typed id survives a poll', list.children[0].value, '17');
check('untouched input still suggested', list.children[1].value, '1');

// and while actually focused, the list is not rebuilt at all
list.children[0].value = '42';
list.children[0].dataset.edited = '1';
ctx.document.activeElement = list.children[0];
render(items, [], 0);
check('no rebuild while focused', list.children[0].value, '42');
ctx.document.activeElement = null;

// a module that gets assigned drops out of the list cleanly
render([{ serial: 'BBBB', age_seconds: 9 }], [], 0);
check('assigned module removed', list.children.length, 1);
check('remaining module suggested', list.children[0].value, '0');

process.exit(failures ? 1 : 0);
