// simple client-side filtering for tables
function filterTable(inputId, tableId) {
  const query = document.getElementById(inputId).value.toLowerCase();
  const rows = document.querySelectorAll(`#${tableId} tbody tr`);
  rows.forEach(r => {
    const text = r.innerText.toLowerCase();
    r.style.display = text.includes(query) ? "" : "none";
  });
}

function csvDownload(filename, csv) {
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const link = document.createElement('a');
  const url = URL.createObjectURL(blob);
  link.setAttribute('href', url);
  link.setAttribute('download', filename);
  link.style.visibility = 'hidden';
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}

// ------------ Column Sorting -------------
function makeTablesSortable() {
  const tables = document.querySelectorAll('table[data-sortable]');
  tables.forEach(table => {
    const thead = table.querySelector('thead');
    if(!thead) return;
    const headers = thead.querySelectorAll('th');
    headers.forEach((th, idx) => {
      if(th.classList.contains('no-sort')) return;
      if(!th.textContent.trim()) { th.classList.add('no-sort'); return; }
      th.classList.add('sortable-col');
      th.addEventListener('click', () => sortTable(table, idx, th));
    });
  });
}

function sortTable(table, colIndex, th) {
  const tbody = table.querySelector('tbody');
  if(!tbody) return;
  const rows = Array.from(tbody.querySelectorAll('tr'));
  const currentDir = th.getAttribute('data-sort-dir') === 'asc' ? 'asc' : th.getAttribute('data-sort-dir') === 'desc' ? 'desc' : null;
  const newDir = currentDir === 'asc' ? 'desc' : 'asc';

  // Clear other headers indicators
  table.querySelectorAll('th[data-sort-dir]').forEach(h => { if(h !== th) { h.removeAttribute('data-sort-dir'); const ic = h.querySelector('.sort-indicator'); if(ic) ic.textContent=''; }});

  // Value extraction & type detection
  const getCellValue = (row) => (row.children[colIndex] ? row.children[colIndex].innerText.trim() : '');
  const sampleValues = rows.slice(0,10).map(r => getCellValue(r)).filter(v => v !== '');
  let type = 'string';
  const numPattern = /^-?\d+(?:[.,]\d+)?$/;
  if(sampleValues.every(v => numPattern.test(v.replace(/,/g,'')))) type = 'number';
  else if(sampleValues.every(v => !isNaN(Date.parse(v)))) type = 'date';

  const collator = new Intl.Collator(undefined, { numeric: type==='string', sensitivity: 'base' });

  rows.sort((a,b) => {
    let va = getCellValue(a), vb = getCellValue(b);
    if(type==='number') {
      va = parseFloat(va.replace(/,/g,'')) || 0; vb = parseFloat(vb.replace(/,/g,'')) || 0;
      return newDir==='asc' ? va - vb : vb - va;
    } else if(type==='date') {
      va = Date.parse(va) || 0; vb = Date.parse(vb) || 0;
      return newDir==='asc' ? va - vb : vb - va;
    } else {
      return newDir==='asc' ? collator.compare(va, vb) : collator.compare(vb, va);
    }
  });

  // Reattach rows
  rows.forEach(r => tbody.appendChild(r));
  th.setAttribute('data-sort-dir', newDir);
  let indicator = th.querySelector('.sort-indicator');
  if(!indicator) {
    indicator = document.createElement('span');
    indicator.className = 'sort-indicator ml-1 text-[10px] text-slate-400';
    th.appendChild(indicator);
  }
  indicator.textContent = newDir === 'asc' ? '▲' : '▼';
}

document.addEventListener('DOMContentLoaded', makeTablesSortable);

// ------------ Pagination -------------
function paginateTables() {
  const tables = document.querySelectorAll('table[data-paginate]');
  tables.forEach(table => setupPagination(table));
}

function setupPagination(table) {
  const storageKey = `tblpg_${table.id || Math.random().toString(36).slice(2)}`;
  const defaultSize = parseInt(table.getAttribute('data-page-size')) || 25;
  let perPage = defaultSize;
  try {
    const saved = localStorage.getItem(storageKey + '_size');
    if(saved) perPage = parseInt(saved) || defaultSize;
  } catch(e) {}
  const tbody = table.querySelector('tbody');
  if(!tbody) return;
  const rows = Array.from(tbody.querySelectorAll('tr'));
  if(rows.length <= perPage) return; // no need
  let current = 1;
  let totalPages = Math.ceil(rows.length / perPage);

  const wrapper = document.createElement('div');
  wrapper.className = 'flex items-center justify-between gap-4 flex-wrap px-3 py-2 text-xs text-slate-600';
  const info = document.createElement('div');
  const controls = document.createElement('div');
  controls.className = 'flex items-center gap-1';
  const sizeWrap = document.createElement('div');
  sizeWrap.className = 'flex items-center gap-1';
  const sizeLabel = document.createElement('span'); sizeLabel.textContent='Rows:';
  const sizeSelect = document.createElement('select');
  sizeSelect.className='soft-input !py-1 !px-2 !h-auto text-xs w-auto';
  ;[10,25,50,100,250].forEach(n=>{
    const opt=document.createElement('option'); opt.value=n; opt.textContent=n; if(n===perPage) opt.selected=true; sizeSelect.appendChild(opt);
  });
  const allOpt=document.createElement('option'); allOpt.value='0'; allOpt.textContent='All'; if(perPage>=rows.length) allOpt.selected=true; sizeSelect.appendChild(allOpt);
  sizeSelect.addEventListener('change', ()=>{
    const val = parseInt(sizeSelect.value);
    perPage = val === 0 ? rows.length : val;
    try { localStorage.setItem(storageKey + '_size', perPage); } catch(e) {}
    totalPages = Math.ceil(rows.length / perPage) || 1;
    current = 1;
    buildControls();
    renderPage();
  });
  sizeWrap.appendChild(sizeLabel); sizeWrap.appendChild(sizeSelect);

  function renderPage() {
    const start = (current-1)*perPage;
    const end = start + perPage;
    rows.forEach((r,i)=>{ r.style.display = (i>=start && i<end) ? '' : 'none'; });
    info.textContent = `Showing ${start+1}–${Math.min(end, rows.length)} of ${rows.length}`;
    controls.querySelectorAll('button.page-btn').forEach(btn => {
      const p = parseInt(btn.getAttribute('data-page'));
      btn.disabled = p === current;
      btn.className = 'page-btn soft-btn bg-slate-100 text-xs ' + (btn.disabled ? 'opacity-60 cursor-default' : 'hover:bg-slate-200');
    });
  }

  function buildControls() {
    controls.innerHTML='';
    const maxButtons = 7; // first, last, current +/-
    let pages = [];
    if(totalPages <= maxButtons) {
      for(let i=1;i<=totalPages;i++) pages.push(i);
    } else {
      pages = [1];
      const windowSize = 3;
      let start = Math.max(2, current - windowSize);
      let end = Math.min(totalPages-1, current + windowSize);
      if(start <= 2) end = 1 + windowSize*2;
      if(end >= totalPages-1) start = totalPages - 1 - windowSize*2;
      for(let i=start;i<=end;i++) pages.push(i);
      pages.push(totalPages);
    }
    let last = null;
    pages.forEach(p => {
      if(last && p-last>1) {
        const gap = document.createElement('span'); gap.textContent='…'; gap.className='px-1'; controls.appendChild(gap);
      }
      const btn = document.createElement('button');
      btn.type='button';
      btn.textContent = p;
      btn.setAttribute('data-page', p);
      btn.className='page-btn soft-btn bg-slate-100 text-xs';
      btn.addEventListener('click', () => { current = p; renderPage(); });
      controls.appendChild(btn);
      last = p;
    });
  }

  const container = table.parentElement; // overflow wrapper or direct
  buildControls();
  renderPage();
  wrapper.appendChild(info);
  wrapper.appendChild(sizeWrap);
  wrapper.appendChild(controls);
  // Insert after table
  container.parentNode.insertBefore(wrapper, container.nextSibling);

  // Resort hook: reapply pagination after sorting
  table.addEventListener('click', e => {
    if(e.target.closest('th.sortable-col')) {
      // After sort, recompute rows array order
      setTimeout(() => {
        const newRows = Array.from(tbody.querySelectorAll('tr'));
        newRows.forEach(r=>r.style.display='');
        current = 1;
        renderPage();
      }, 0);
    }
  });
}

document.addEventListener('DOMContentLoaded', paginateTables);
