// The smallest DOM the provisioning renderer needs: elements with a dataset
// and classList, innerHTML that parses back the one input shape the renderer
// emits, and querySelectorAll/contains. Not a browser — just enough to assert
// what happens to a value across a re-render.
// Enough DOM for renderUnprovisionedModules: elements, innerHTML parsing of
// the one shape it emits, querySelectorAll, dataset, activeElement.
class El {
  constructor(cls=''){ this.dataset={}; this.value=''; this._html=''; this.children=[];
    this.classList={ _s:new Set(cls?cls.split(' '):[]),
      contains:c=>this.classList._s.has(c), add:c=>this.classList._s.add(c) }; }
  get innerHTML(){ return this._html; }
  set innerHTML(v){
    this._html = v; this.children = [];
    // Serial from data-serial (new markup) or the id attribute (old markup),
    // so the same harness can drive both versions.
    const re = /<input class="provision-id-input" id="provision-id-([^"]*)"[\s\S]*?value="([^"]*)"/g;
    let m; while((m = re.exec(v))){
      const inp = new El('provision-id-input');
      inp.dataset.serial = m[1]; inp.value = m[2]; this.children.push(inp);
    }
  }
  querySelectorAll(sel){ return this.children.filter(c=>c.classList.contains(sel.replace('.',''))); }
  contains(node){ return this.children.includes(node); }
}
module.exports = { El };
