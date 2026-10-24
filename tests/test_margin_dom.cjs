// Synthetic DOM only; no browser, network, account or trade operations.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../bot_core/margin_guard.py'), 'utf8');
const script = source.match(/READ_QUOTE_JS = r"""([\s\S]*?)"""/)[1];
function element(text, left, right, top, bottom, hidden=false) {
  return {innerText:text, childElementCount:0, children:[], hidden,
          getBoundingClientRect:()=>({left,right,top,bottom,width:right-left,height:bottom-top})};
}
function read(elements, button) {
  const context = {document:{querySelectorAll:()=>elements},
                   getComputedStyle:el=>({visibility:el.hidden?'hidden':'visible',display:'block'})};
  return vm.runInNewContext('('+script+')',context)(button);
}
const av=element('Available To Trade\n2.21 USD',1680,2045,120,145);
const buyCost=element('Cost: 17.93 USD',1687,1862,690,710);
const sellCost=element('Cost: 17.82 USD',1870,2045,690,710);
const combined=element('Cost: 17.93 USD\nCost: 17.82 USD',1687,2045,690,710);
const hidden=element('Available To Trade 9999 USD',1680,2045,120,145,true);
const unrelated=element('Cost: 1 USD',1870,2045,900,920);
const elements=[av,buyCost,sellCost,combined,hidden,unrelated];
const sell=element('Sell',1870,2045,752,796);
const buy=element('Buy',1687,1862,752,796);
assert.equal(read(elements,sell).cost_text,'Cost: 17.82 USD');
assert.equal(read(elements,buy).cost_text,'Cost: 17.93 USD');
assert.equal(read(elements,sell).available_text,'Available To Trade\n2.21 USD');
assert.equal(read([av,combined],sell).side_cost_bound,false);
const varCost=element('Required Margin\n$17.82',1700,2040,690,715);
assert.equal(read([av,varCost],sell).cost_text,'Required Margin\n$17.82');
const noMargin=read([element('Equity 100 USD',1680,2045,120,145),sellCost],sell);
assert.equal(noMargin.available_text,'');
assert.equal(read([av,unrelated],sell).side_cost_bound,false);
process.stdout.write(JSON.stringify({tests:7,passed:7,synthetic_dom_only:true})+'\n');
